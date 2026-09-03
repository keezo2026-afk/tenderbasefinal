"""The scope table is a security boundary, so it is tested rather than trusted.

Three things have to stay true at once, and none of them is enforced by mypy:

1. ``ROUTER_SCOPES`` (what actually gates a request, as a router-level dependency)
   and ``SCOPE_REQUIREMENTS`` (the historical path-prefix map that :func:`api_access`
   still falls back to) describe the same permissions. The router docstring promises
   they cannot drift; this is where that promise is kept.
2. Every route the API documents sits under a family that has a declared scope. A new
   router included without a dependency is the classic way to ship an unauthenticated
   data endpoint, and the fallback map alone would hide it behind ``read:tenders``.
3. The scope a client is *told* it needs is the scope that gated the call. A 403 that
   names the wrong scope is worse than no message: the integrator fixes the wrong thing.

The behavioural half mints real keys and drives real HTTP, because a dependency wired
onto the wrong router passes any amount of table comparison.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.v1.router import ROUTER_SCOPES
from app.enums import SCOPE_REQUIREMENTS, ApiKeyScope
from tests.integration.test_api_security import issue_key

#: ``engine`` creates the schema on this backend; these tests drive real HTTP.
pytestmark = pytest.mark.usefixtures("engine")

#: Router attribute name → (mount prefix under ``/api/v1``, cheapest parameterless GET to
#: probe it with). ``operations`` has no collection route, so an existing report endpoint
#: stands in. Both columns are asserted against the OpenAPI document below, so this table
#: cannot quietly rot when a route moves.
ROUTES: dict[str, tuple[str, str]] = {
    "tenders": ("/tenders", "/api/v1/tenders"),
    "search": ("/search", "/api/v1/search"),
    "events": ("/events", "/api/v1/events"),
    "municipalities": ("/municipalities", "/api/v1/municipalities"),
    "provinces": ("/provinces", "/api/v1/provinces"),
    "categories": ("/categories", "/api/v1/categories"),
    "documents": ("/documents", "/api/v1/documents"),
    "sources": ("/sources", "/api/v1/sources"),
    "statistics": ("/statistics", "/api/v1/statistics"),
    "operations": ("/operations", "/api/v1/operations/sources/freshness"),
    "api_keys": ("/api-keys", "/api/v1/api-keys"),
}

PROBES: dict[str, str] = {router: probe for router, (_, probe) in ROUTES.items()}
PREFIXES: dict[str, str] = {router: prefix for router, (prefix, _) in ROUTES.items()}

#: The mutating surface, spelled out so an addition has to be argued for here first.
#: Key minting and revocation are admin-scoped, and reconciliation is the one
#: operations route that writes (it tightens the router's ``read:sources`` itself).
MUTATING_OPERATIONS: frozenset[str] = frozenset(
    {
        "post /api/v1/api-keys",
        "post /api/v1/api-keys/{key_id}/revoke",
        "post /api/v1/operations/reconcile",
    }
)


def _documented_paths(client: Any) -> dict[str, Any]:
    return dict(client.app.openapi()["paths"])


# ---------------------------------------------------------------------------
# 1. the two tables agree
# ---------------------------------------------------------------------------


def test_the_scope_tables_describe_the_same_permissions() -> None:
    """``ROUTER_SCOPES`` and the ``SCOPE_REQUIREMENTS`` fallback name one scope per family."""
    assert set(ROUTES) == set(ROUTER_SCOPES), "the router table and this probe table disagree"
    assert set(PREFIXES.values()) == set(SCOPE_REQUIREMENTS), (
        "a family exists in one table but not the other: a router could be gated by a scope "
        "the fallback map does not know, or the fallback map guards a router that is gone"
    )
    for router, scope in ROUTER_SCOPES.items():
        prefix = PREFIXES[router]
        assert SCOPE_REQUIREMENTS[prefix] == str(scope), (
            f"{prefix}: the router requires {scope!s} but the path fallback says "
            f"{SCOPE_REQUIREMENTS[prefix]}, so the same endpoint is gated two ways depending on "
            "which code path reads the request"
        )


def test_the_documented_oddity_stays_documented() -> None:
    """``/municipalities`` intentionally asks for ``read:tenders``, not ``read:geography``.

    Geography routes exist so a tender consumer can resolve a filter value; tightening
    this would invalidate working keys without reducing what an attacker can read. That
    reasoning is written in ``app/api/v1/router.py``, and this test is what stops the
    decision from being "fixed" by accident.
    """
    assert ROUTER_SCOPES["municipalities"] is ApiKeyScope.READ_TENDERS
    assert SCOPE_REQUIREMENTS["/municipalities"] == str(ApiKeyScope.READ_TENDERS)
    assert ROUTER_SCOPES["provinces"] is ApiKeyScope.READ_GEOGRAPHY
    # Reconciliation writes, so it is the one operations route that demands admin.
    assert ROUTER_SCOPES["operations"] is ApiKeyScope.READ_SOURCES
    assert ROUTER_SCOPES["api_keys"] is ApiKeyScope.ADMIN


# ---------------------------------------------------------------------------
# 2. nothing is mounted outside the table
# ---------------------------------------------------------------------------


async def test_every_documented_route_belongs_to_a_scoped_family(make_client) -> None:  # noqa: ANN001
    """No ``/api/v1`` route may live outside a family that declares a scope.

    Plus the reverse direction: each mount prefix and each probe path in this module's
    table is still real, so the checks above cannot pass against a stale snapshot.
    """
    client = await make_client(api_key_enforcement_enabled=True)
    paths = _documented_paths(client)

    v1 = [path for path in paths if path.startswith("/api/v1/")]
    assert len(v1) >= 30, (
        f"only {len(v1)} routes documented — the schema is not what this test expects"
    )

    mounted = {f"/api/v1{prefix}" for prefix in PREFIXES.values()}
    for path in v1:
        if path.startswith("/api/v1/health"):
            continue  # orchestrator probes, deliberately public
        assert any(path.startswith(prefix) for prefix in mounted), (
            f"{path} is served under a prefix with no declared scope"
        )

    for router, (prefix, probe) in ROUTES.items():
        assert any(p == f"/api/v1{prefix}" or p.startswith(f"/api/v1{prefix}/") for p in paths), (
            f"{router} is mounted at {prefix} in this test's table but the API no longer serves it"
        )
        assert probe in paths, f"probe for {router} is no longer documented: update ROUTES"


async def test_only_the_documented_mutations_exist(make_client) -> None:  # noqa: ANN001
    """The public API is read-only apart from key management and reconciliation."""
    client = await make_client(api_key_enforcement_enabled=True)

    mutations = {
        f"{method} {path}"
        for path, operations in _documented_paths(client).items()
        for method in operations
        if method not in {"get", "head", "options"}
    }

    assert mutations == set(MUTATING_OPERATIONS), (
        "the mutating surface changed: docs/SECURITY.md promises these three operations and no more"
    )


# ---------------------------------------------------------------------------
# 3. enforcement is real, per family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("router", sorted(ROUTES))
async def test_a_key_needs_exactly_its_family_scope(make_client, session, router: str) -> None:  # noqa: ANN001
    """One scope opens one family, and a refusal names that scope.

    Positive and negative halves in the same test on purpose: asserting only the 403 would
    also pass if every endpoint were closed, and asserting only the 200 would pass if
    authentication had been switched off — both of which are how this table goes wrong.
    """
    required = str(ROUTER_SCOPES[router])
    # A *read* scope deliberately: ``admin`` grants everything (``ApiKey.grants``), so it
    # would not be a negative case.
    other = "read:documents" if required != "read:documents" else "read:geography"
    client = await make_client(api_key_enforcement_enabled=True)
    path = PROBES[router]

    wrong = await issue_key(client, session, scopes=[other])
    refused = await client.get(path, headers={"X-API-Key": wrong})
    assert refused.status_code == 403, f"{path} opened for a key holding only {other}"
    error = refused.json()["error"]
    assert error["code"] == "INSUFFICIENT_SCOPE"
    assert required in error["message"] + str(error.get("details")), (
        f"a caller of {path} is told {error['message']!r} instead of {required}"
    )

    right = await issue_key(client, session, scopes=[required])
    allowed = await client.get(path, headers={"X-API-Key": right})
    # 422 is acceptable: the gate is open, the request may simply be under-specified.
    assert allowed.status_code not in {401, 403}, (
        f"{path} refused a key holding {required}: {allowed.status_code} {allowed.text[:200]}"
    )


async def test_anonymous_callers_are_rejected_alike(make_client) -> None:  # noqa: ANN001
    """Every scoped family answers an anonymous call with the same bare 401.

    Uniformity is the point: a 404 on one family and a 403 on another tells an unauthenticated
    caller which endpoints exist, and an accidental 200 is the bug nobody notices until the data
    is public.
    """
    client = await make_client(api_key_enforcement_enabled=True)

    for path in sorted(PROBES.values()):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code} anonymously"
        body = response.json()
        assert body["error"]["code"] == "API_KEY_MISSING", path
        assert body["error"]["request_id"], "a rejection without a request_id cannot be traced"

    probes = await client.get("/api/v1/health")
    assert probes.status_code == 200, "the orchestrator probe must stay reachable anonymously"
