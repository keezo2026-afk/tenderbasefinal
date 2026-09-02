"""FastAPI application factory.

The API is the presentation layer only: it depends on services, which depend on
the database. No route talks to SQLAlchemy models directly in its response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging, get_logger

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

### Data provenance

Each opportunity links back to its `source_url` and its registered source, so
any record can be verified against the original publication.
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness, readiness and dependency health."},
    {"name": "tenders", "description": "Procurement opportunities, documents, events, versions."},
    {"name": "search", "description": "Full-text search across opportunities."},
    {"name": "municipalities", "description": "Municipal hierarchy and per-municipality tenders."},
    {"name": "provinces", "description": "Provinces and district municipalities."},
    {"name": "sources", "description": "Source registry, connectors and ingestion runs."},
    {"name": "documents", "description": "Document metadata, versions and extracted text."},
    {"name": "categories", "description": "Procurement category taxonomy."},
    {"name": "events", "description": "Cross-opportunity change feed."},
    {"name": "statistics", "description": "Aggregate platform statistics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shutdown hooks."""
    settings: Settings = app.state.settings
    logger = get_logger("tenderbase.app")
    # Importing the package registers every built-in connector.
    import app.connectors  # noqa: F401
    from app.connectors.registry import registered_keys

    logger.info(
        "app.startup",
        environment=settings.app_env,
        version=settings.app_version,
        connectors=list(registered_keys()),
        ai_enabled=settings.ai_enabled,
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("app.shutdown")


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
    )
    app.state.settings = cfg

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=cfg.api_v1_prefix)

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
                }
            }
        )

    return app


app = create_app()
