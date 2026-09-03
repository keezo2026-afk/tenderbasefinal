"""Authentication and rate-limit dependencies for the versioned API.

One dependency — :func:`api_access` — owns the whole request-authorization
flow for protected data endpoints:

1. resolve the presented key (``X-API-Key`` header, or ``Authorization:
   Bearer`` for clients that only speak bearer tokens);
2. require the scope that the endpoint's path demands
   (see :data:`app.enums.SCOPE_REQUIREMENTS`);
3. record last-used metadata;
4. apply the Redis-backed per-key rate limit.

Health probes and documentation routes are deliberately **not** protected:
an orchestrator cannot hold a credential, and a probe failure caused by
authentication is an outage of the wrong thing.

Public (unauthenticated) endpoints
----------------------------------
``GET /``, ``/api/v1/health``, ``/api/v1/health/live``,
``/api/v1/health/ready``, ``/api/v1/stats``-free docs routes
(``/api/docs``, ``/api/redoc``, ``/openapi.json``) and ``/metrics``
(optionally token-protected). Everything under ``/api/v1/**`` that returns data
requires a key when ``enforce_api_keys`` is on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.context import SettingsDep
from app.config import Settings
from app.db.models.security import ApiKey
from app.db.session import get_session
from app.enums import SCOPE_REQUIREMENTS, ApiKeyScope
from app.errors import RateLimitedError, TenderBaseError
from app.logging import get_logger
from app.observability import metrics
from app.services.api_key_service import ApiKeyService, AuthenticationError
from app.services.rate_limit import RateDecision, get_limiter, policy_for

logger = get_logger("tenderbase.api.auth")

#: Declared as FastAPI security schemes so the generated OpenAPI document (and
#: therefore Swagger UI / ReDoc) shows the padlock and the ``X-API-Key`` input on
#: every protected operation, without each route restating it.
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="APIKeyHeader")
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


#: Authentication resolves its session through *exactly* the callable the routes
#: use — an alias, not a wrapper. FastAPI caches a dependency by identity, so a
#: wrapper function would open a second AsyncSession (a second pooled connection)
#: for every authenticated request and quietly halve the pool under load. As an
#: alias, one request checks out one connection, and a test that overrides
#: ``get_session`` redirects authentication and data access together.
get_auth_session = get_session


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller (or an explicitly anonymous one)."""

    key: ApiKey | None = None
    tier: str = "anonymous"

    @property
    def authenticated(self) -> bool:
        return self.key is not None

    @property
    def display_id(self) -> str | None:
        """Safe identifier for logs — a prefix, never a secret."""
        if self.key is None:
            return None
        return f"{self.key.key_prefix}({str(self.key.id)[:8]})"

    def grants(self, scope: str | None) -> bool:
        if scope is None:
            return True
        if self.key is None:
            return False
        return self.key.grants(scope)


def required_scope_for(path: str) -> str | None:
    """Map a request path to the scope it needs (``None`` = public).

    Matching is on the first path segment below ``/api/v1`` so that nested
    resources inherit their collection's scope (``/tenders/{id}/documents``
    still needs ``read:tenders``; the document is reached through the tender).
    """
    stripped = path.rstrip("/")
    for prefix in ("/api/v1", "/api"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    else:
        return None
    segments = [segment for segment in unquote(stripped).split("/") if segment]
    if not segments:
        return None
    head = f"/{segments[0]}"
    if head in ("/health", "/metrics", "/docs", "/redoc"):
        return None
    return SCOPE_REQUIREMENTS.get(head, str(ApiKeyScope.READ_TENDERS))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Left-most entry is the client as seen by the first proxy we trust.
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def extract_credentials(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_scheme)] = None,
    credentials: Annotated[Any, Security(bearer_scheme)] = None,
) -> str | None:
    """Pull the API key out of the request.

    Both transports exist because data consumers are heterogeneous: HTTP clients
    happily send a custom header, but many SDK/gateway configurations can only
    send a bearer token. The value is never logged and never echoed back.
    """
    if api_key:
        return api_key.strip()
    token = getattr(credentials, "credentials", None)
    if token:
        return token.strip()
    return None


CredentialDep = Annotated[str | None, Depends(extract_credentials)]


async def _limit(
    request: Request, principal: Principal, *, settings: Settings, bucket: str | None = None
) -> RateDecision | None:
    limiter = get_limiter()
    if limiter is None or not settings.use_rate_limit:
        return None
    tier = principal.tier
    limit, burst = policy_for(tier, settings)
    key = bucket or (principal.display_id if principal.authenticated else _client_ip(request))
    decision = await limiter.check(f"{tier}:{key}", limit=limit, burst=burst)
    request.state.rate_limit = decision
    metrics.RATE_LIMIT_DECISIONS.labels(
        tier=tier, outcome="allow" if decision.allowed else "block", backend=decision.backend
    ).inc()
    if not decision.allowed:
        metrics.RATE_LIMIT_REJECTS.labels(tier=tier).inc()
        raise RateLimitedError(
            "Rate limit exceeded. Retry after the window resets.",
            code="RATE_LIMITED",
            details={
                "limit_per_minute": decision.limit,
                "retry_after_seconds": decision.retry_after_seconds,
            },
            headers={"Retry-After": str(max(decision.retry_after_seconds, 1))},
        )
    return decision


def attach_rate_headers(response_headers: dict[str, str], decision: RateDecision | None) -> None:
    if decision is not None:
        response_headers.update(decision.headers())


async def api_access(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_auth_session)],
    settings: SettingsDep,
    credential: CredentialDep = None,
) -> Principal:
    """Authenticate + authorise + rate-limit one protected request."""
    service = ApiKeyService(session, settings)
    required = required_scope_for(request.url.path)

    if not settings.enforce_api_keys:
        # Unauthenticated deployment mode (local development / tests). Limits,
        # when enabled, are still applied per client IP.
        principal = Principal(key=None, tier="anonymous")
        await _limit(request, principal, settings=settings)
        return principal

    if credential is None:
        metrics.AUTH_REJECTIONS.labels(code="API_KEY_MISSING").inc()
        raise AuthenticationError(
            "Missing API key. Send it in the X-API-Key header.",
            code="API_KEY_MISSING",
        )
    try:
        key = await service.verify(credential, required_scope=required)
    except TenderBaseError as exc:
        # Every deliberate refusal — missing key, unknown key, expired, revoked
        # (401) and insufficient scope (403) — keeps its own status and code.
        # Catching only AuthenticationError here turned a 403 into a 503, which
        # tells an integrator to retry a request that will never succeed.
        metrics.AUTH_REJECTIONS.labels(code=str(exc.code)).inc()
        request.state.auth_error = str(exc.code)
        raise
    except Exception:
        # A database failure during auth must not be reported as "invalid key".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable",
        ) from None

    if key.grants(str(ApiKeyScope.ADMIN)):
        principal = Principal(key=key, tier="admin")
    else:
        principal = Principal(key=key, tier="authenticated")

    _ = await _limit(request, principal, settings=settings)
    try:
        await service.touch(key, client_ip=_client_ip(request))
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - audit is best effort
        logger.warning("auth.touch_commit_failed", error=str(exc))
        await session.rollback()
    request.state.principal = principal
    return principal
