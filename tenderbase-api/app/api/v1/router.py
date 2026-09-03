"""API v1 router aggregation — and the scope table for the whole public API.

Health probes are the only routes without the authentication dependency: an
orchestrator (Kubernetes, ECS, Fly, a load balancer) cannot present a
credential, and blocking it turns a healthy service into a false outage.

``/metrics`` is mounted by the application factory, not here — it is an internal
operations surface and is kept out of the public OpenAPI schema on purpose.

Scopes are declared per router, explicitly, rather than inferred from the request
path. ``app.enums.SCOPE_REQUIREMENTS`` still exists as the fallback used by
:func:`~app.api.auth.api_access` (and by anything mounted without a dependency
here), and this table is asserted against it in the tests, so the two cannot
drift: the value that authorises a request is the value a reader of this file
sees. Two deliberate inheritances from the historical mapping are kept:
``/municipalities`` needs ``read:tenders`` rather than ``read:geography``,
because the geography routers are how a tender consumer resolves a filter, and
tightening it would break existing keys for no security gain.
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.auth import require_scope
from app.api.v1.routes import (
    api_keys,
    categories,
    documents,
    events,
    health,
    municipalities,
    operations,
    provinces,
    search,
    sources,
    statistics,
    tenders,
)
from app.enums import ApiKeyScope


def protected(scope: ApiKeyScope) -> list[Any]:
    """The router-level dependencies for a router that needs exactly ``scope``.

    ``list[Any]`` rather than ``list[Depends]``: ``Depends`` is a dataclass FastAPI
    accepts *as a type* only in annotations it introspects, and a return annotation is
    not one of them.
    """
    return [Depends(require_scope(scope))]


api_router = APIRouter()

api_router.include_router(health.router)

#: One line per router: prefix → required scope. Read alongside
#: ``docs/API.md``'s authentication table.
ROUTER_SCOPES: dict[str, ApiKeyScope] = {
    "tenders": ApiKeyScope.READ_TENDERS,
    "search": ApiKeyScope.READ_TENDERS,
    "events": ApiKeyScope.READ_TENDERS,
    "municipalities": ApiKeyScope.READ_TENDERS,
    "provinces": ApiKeyScope.READ_GEOGRAPHY,
    "categories": ApiKeyScope.READ_GEOGRAPHY,
    "documents": ApiKeyScope.READ_DOCUMENTS,
    "sources": ApiKeyScope.READ_SOURCES,
    "statistics": ApiKeyScope.READ_STATISTICS,
    "operations": ApiKeyScope.READ_SOURCES,
    "api_keys": ApiKeyScope.ADMIN,
}

api_router.include_router(tenders.router, dependencies=protected(ROUTER_SCOPES["tenders"]))
api_router.include_router(search.router, dependencies=protected(ROUTER_SCOPES["search"]))
api_router.include_router(events.router, dependencies=protected(ROUTER_SCOPES["events"]))
api_router.include_router(
    municipalities.router, dependencies=protected(ROUTER_SCOPES["municipalities"])
)
api_router.include_router(provinces.router, dependencies=protected(ROUTER_SCOPES["provinces"]))
api_router.include_router(categories.router, dependencies=protected(ROUTER_SCOPES["categories"]))
api_router.include_router(documents.router, dependencies=protected(ROUTER_SCOPES["documents"]))
api_router.include_router(sources.router, dependencies=protected(ROUTER_SCOPES["sources"]))
api_router.include_router(statistics.router, dependencies=protected(ROUTER_SCOPES["statistics"]))
# Reconciliation is the one mutating operations route and needs ``admin``; the router
# grants ``read:sources`` for its reports, so the route tightens it itself.
api_router.include_router(operations.router, dependencies=protected(ROUTER_SCOPES["operations"]))
api_router.include_router(api_keys.router, dependencies=protected(ROUTER_SCOPES["api_keys"]))

__all__ = ["ROUTER_SCOPES", "api_router", "protected"]
