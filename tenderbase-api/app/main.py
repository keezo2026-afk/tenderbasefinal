"""FastAPI application factory.

The API is the presentation layer only: it depends on services, which depend on
the database. No route talks to SQLAlchemy models directly in its response.

Startup deliberately treats its dependencies by importance:

* **PostgreSQL is required** — the read API without data is meaningless, so a
  database that cannot be reached is a failed start.
* **Redis is optional** — it fronts the ingestion queue and the distributed rate
  limiter. If it is down the API keeps serving reads and the limiter degrades to
  a per-process window; that is a *degraded* deployment, not a dead one.
* **AI is optional** and off by default (see :mod:`app.ai`).

Endpoints mounted here: the versioned API, the OpenAPI documents, and
``/metrics`` — which is *not* in the public OpenAPI schema, because an operations
endpoint should not be advertised to the internet.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.errors import register_exception_handlers
from app.api.middleware import PublicRateLimitMiddleware, RequestContextMiddleware
from app.api.v1.router import api_router
from app.api.v1.routes.health import router as health_router
from app.config import Settings, get_settings
from app.db.session import dispose_engine, get_sessionmaker
from app.logging import configure_logging, get_logger
from app.observability import metrics
from app.services.rate_limit import build_limiter, get_limiter, install_limiter

DESCRIPTION = """
**TenderBase** is a normalized procurement-intelligence data platform for South
African public-sector procurement, exposed as a versioned REST API.

Every record answers: *what is the opportunity, who published it, where did it
come from, when was it published, when does it close, what changed, which
documents belong to it, and how confident are we in the extracted data?*

### Conventions

* All responses use a consistent envelope: `data`, `pagination`, `meta`.
* Errors always return `{"error": {"code", "message", "request_id"}}`.
* Every response carries an `X-Request-ID` header for support and debugging.
* All timestamps are timezone-aware **UTC** (ISO-8601). The source timezone is
  preserved on each record.
* Pagination is deterministic (`page`, `page_size`) with a server-enforced
  maximum page size.
* Missing data is `null` — the platform never fabricates values.
* Development fixtures are flagged with `is_test_fixture` and excluded by
  default.

### Authentication

Every data endpoint requires an API key in the `X-API-Key` header (an
`Authorization: Bearer <key>` header is accepted too). Keys carry scopes; a key
without the scope an endpoint needs is rejected with `403 INSUFFICIENT_SCOPE`.
`GET /api/v1/health*` and the documentation are always public — orchestrators
cannot hold credentials.

| Scope | Grants |
| --- | --- |
| `read:tenders` | `/tenders`, `/search`, `/events`, `/municipalities` |
| `read:geography` | `/provinces`, `/categories` |
| `read:documents` | `/documents` |
| `read:sources` | `/sources`, `/operations` |
| `read:statistics` | `/statistics` |
| `admin` | all of the above plus `/api-keys` |

Keys are issued with `python -m scripts.manage_api_keys create --name ...`; only
a keyed digest is stored, so a lost key is replaced, never recovered.

### Rate limiting

`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` accompany every
response when limiting is on; exceeding the budget returns `429` with
`Retry-After`. Limits are per tier (anonymous / authenticated / admin) and
enforced in Redis so a client's budget is shared across replicas.

### Data provenance

Each opportunity links back to its `source_url` and its registered source, so any
record can be verified against the original publication.
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness, readiness and dependency health. Always public."},
    {"name": "tenders", "description": "Procurement opportunities, documents, events, versions."},
    {"name": "search", "description": "Full-text search across opportunities."},
    {"name": "municipalities", "description": "Municipal hierarchy and per-municipality tenders."},
    {"name": "provinces", "description": "Provinces and district municipalities."},
    {"name": "sources", "description": "Source registry, connectors and ingestion runs."},
    {"name": "documents", "description": "Document metadata, versions and extracted text."},
    {"name": "categories", "description": "Procurement category taxonomy."},
    {"name": "events", "description": "Cross-opportunity change feed."},
    {"name": "statistics", "description": "Aggregate platform statistics."},
    {"name": "api-keys", "description": "API key administration (admin scope only)."},
    {
        "name": "operations",
        "description": "Ingestion run reports, failure triage and duplicate review.",
    },
]


def _token_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison; a metrics token is a credential."""
    return hmac.compare_digest(presented.encode(), expected.encode())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shutdown hooks."""
    settings: Settings = app.state.settings
    logger = get_logger("tenderbase.app")
    # Importing the package registers every built-in connector.
    import app.connectors  # noqa: F401
    from app.connectors.registry import registered_keys

    # The limiter is built before serving: it probes Redis once and chooses
    # distributed or local enforcement for the lifetime of the process.
    if settings.use_rate_limit:
        try:
            install_limiter(await build_limiter(settings))
        except Exception as exc:  # noqa: BLE001 - a bad limiter must not block the API
            logger.error("app.rate_limiter_init_failed", error=str(exc))

    await _verify_database(logger, settings)

    logger.info(
        "app.startup",
        environment=settings.app_env,
        version=settings.app_version,
        connectors=list(registered_keys()),
        ai_enabled=settings.ai_enabled,
        auth_enabled=settings.enforce_api_keys,
        rate_limit_enabled=settings.use_rate_limit,
        rate_limit_backend=getattr(get_limiter(), "name", "disabled"),
    )
    try:
        yield
    finally:
        limiter = get_limiter()
        if limiter is not None:
            await limiter.close()
            install_limiter(None)
        await dispose_engine()
        logger.info("app.shutdown")


async def _verify_database(logger, settings: Settings) -> None:  # noqa: ANN001
    """Fail fast (but informatively) when the database is unreachable."""
    from sqlalchemy import text

    try:
        async with get_sessionmaker(settings)() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - logged, then readiness reports it
        logger.error(
            "app.database_unreachable",
            error=str(exc)[:400],
            hint="Run `alembic upgrade head` and check DATABASE_URL.",
        )
        # Not fatal: a container started slightly before its database should
        # still come up and report not-ready rather than crash-loop on a race.


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    cfg = settings or get_settings()
    configure_logging(cfg)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        docs_url=f"{cfg.api_prefix}/docs",
        redoc_url=f"{cfg.api_prefix}/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        contact={"name": "TenderBase API", "url": "https://github.com/"},
        license_info={"name": "Apache-2.0"},
        servers=[],
    )
    app.state.settings = cfg
    # Set at build time rather than only in the lifespan, so ``tenderbase_build_info``
    # is present for anything that imports the app without running startup (a
    # worker, a test client, a scrape during a restart) and never renders an
    # empty Info metric.
    metrics.set_build_info(cfg)

    # Declaration only — the routers attach the dependency; this makes Swagger UI
    # and ReDoc show the padlock and the per-operation scope requirement.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(PublicRateLimitMiddleware)
    if cfg.cors_origins:
        # CORS stays disabled unless an operator lists trusted origins. There is
        # no first-party browser client, so "*" with credentials would be a
        # cross-origin data-exfiltration path for nothing.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["Authorization", "X-API-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Limit"],
        )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=cfg.api_v1_prefix)
    # Health probes are mounted a second time at the application root
    # (``/health/live``, ``/health/ready``). Container orchestrators, classic
    # load balancers and the Docker ``HEALTHCHECK`` all want an unauthenticated
    # path that does not carry the API version; keeping the canonical
    # ``/api/v1/health*`` paths as well means neither audience has to be
    # special-cased. They are hidden from OpenAPI to avoid duplicate tags.
    app.include_router(health_router, include_in_schema=False)
    _mount_metrics(app, cfg)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        """Service banner pointing at the documentation."""
        return JSONResponse(
            {
                "data": {
                    "name": cfg.app_name,
                    "version": cfg.app_version,
                    "documentation": f"{cfg.api_prefix}/docs",
                    "openapi": "/openapi.json",
                    "health": f"{cfg.api_v1_prefix}/health",
                    "authentication": (
                        "X-API-Key header"
                        if cfg.enforce_api_keys
                        else "disabled in this environment"
                    ),
                }
            }
        )

    return app


def _mount_metrics(app: FastAPI, cfg: Settings) -> None:
    """Expose Prometheus metrics on an internal, non-advertised path."""
    if not cfg.metrics_enabled:

        @app.get(cfg.metrics_path, include_in_schema=False)
        async def metrics_disabled() -> Response:
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        return

    # HTTP request/latency metrics are recorded by RequestContextMiddleware
    # against the declared collectors (see app/observability/metrics.py for why
    # they are not delegated to an instrumentation library).
    @app.get(cfg.metrics_path, include_in_schema=False, response_class=PlainTextResponse)
    async def metrics_endpoint(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> Response:
        """Prometheus text exposition. Not part of the public OpenAPI schema."""
        if cfg.metrics_token:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not _token_matches(presented, cfg.metrics_token):
                # 401 + WWW-Authenticate, not 403: "you have not authenticated"
                # is the truth here, and a scrape target that answers 403 makes
                # Prometheus report a permanent configuration error instead of
                # retrying with credentials. Both branches give the identical
                # response so the endpoint cannot be probed for token length.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Metrics require the configured bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        try:
            from app.observability.snapshot import refresh

            await refresh(cfg)
        except Exception:  # noqa: BLE001 - a scrape must never fail on a gauge
            pass
        response.headers["Cache-Control"] = "no-store"
        return Response(content=metrics.render_metrics(), media_type="text/plain; version=0.0.4")


app = create_app()
