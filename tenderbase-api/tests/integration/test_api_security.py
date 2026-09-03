"""Authentication, scope enforcement, rate limiting and metrics — over real HTTP.

The unit tests for these components exercise the services directly. This module
drives the ASGI application so that the *wiring* is under test, because the
wiring is where security guarantees are usually lost:

* which routes are gated, and which are deliberately public (probes);
* whether ``enforce_api_keys`` on an app built with those settings actually
  rejects anonymous data access — rather than being read from a process-global
  that the deployment never configured;
* that the rate limiter installed at startup gates requests, returns
  ``Retry-After`` when it refuses, and says which backend decided;
* that no response, log field or OpenAPI document leaks a stored secret.

Development data: keys minted here exist only inside a per-test database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models.security import ApiKey
from app.services.api_key_service import ApiKeyService
from app.services.rate_limit import build_limiter, install_limiter

#: ``engine`` creates the schema on this backend; these tests drive real HTTP
#: against a second app instance, so they need the tables but not the DB fixture.
pytestmark = pytest.mark.usefixtures("engine")

PROTECTED = "/api/v1/tenders"
PUBLIC = "/api/v1/health"
#: ``redis://127.0.0.1:1`` refuses the connection immediately instead of
#: hanging on a firewall timeout, so the limiter takes its documented fallback
#: path within milliseconds.
DEAD_REDIS = "redis://127.0.0.1:1/0"


def settings_of(client: AsyncClient) -> Any:
    """The settings object the application under test was built with."""
    return client.app.state.settings


async def issue_key(
    client: AsyncClient,
    session: Any,
    *,
    scopes: list[str] | str = "read:tenders",
    expire_in: timedelta | None = None,
    revoke: bool = False,
) -> str:
    """Mint a key with the app's own settings and return the raw secret.

    The same :class:`Settings` are used because the stored credential is a keyed
    digest of the secret: minting with a different pepper than the application
    under test would produce a 401 that looks like an authentication bug.

    ``expire_in`` may be negative — the service refuses to *issue* an expired key
    (a useful invariant), so a stale credential is produced by back-dating a
    valid one, which is how a real key goes stale.
    """
    now = datetime.now(UTC)
    service = ApiKeyService(session, settings_of(client))
    issued = await service.create(
        name=f"test {uuid4().hex[:8]}",
        scopes=[scopes] if isinstance(scopes, str) else scopes,
        expires_at=None if expire_in is None or expire_in <= timedelta(0) else now + expire_in,
        created_by="tests/integration/test_api_security.py",
    )
    row = await _row_for(session, issued.key_id)
    if expire_in is not None and expire_in <= timedelta(0):
        row.expires_at = now + expire_in
    if revoke:
        row.status = "REVOKED"
        row.revoked_at = now
    await session.commit()
    return issued.raw_key


async def _row_for(session: Any, key_id: Any) -> Any:
    return (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()


@asynccontextmanager
async def limiter_installed(settings: Any, **overrides: Any):
    """Install a limiter for this app's settings, restoring global state after.

    The application normally installs it in its lifespan; the test transport
    does not run lifespan events, so the tests do it explicitly against the
    same :func:`build_limiter` the startup hook calls.
    """
    limiter = await build_limiter(settings.model_copy(update=dict(overrides)))
    install_limiter(limiter)
    try:
        yield limiter
    finally:
        await limiter.close()
        install_limiter(None)


# -- authentication ---------------------------------------------------------


async def test_protected_data_requires_a_key_when_enforcement_is_on(make_client) -> None:
    client = await make_client(api_key_enforcement_enabled=True)

    response = await client.get(PROTECTED)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "API_KEY_MISSING"
    # The message is the documentation a confused integrator actually reads.
    assert "X-API-Key" in error["message"]


async def test_health_probes_stay_public_even_when_keys_are_required(make_client) -> None:
    client = await make_client(api_key_enforcement_enabled=True)

    assert (await client.get(PUBLIC)).status_code == 200
    assert (await client.get(f"{PUBLIC}/live")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get(PROTECTED)).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "tb_live_not_a_real_key_at_all"},
        {"Authorization": "Bearer tb_live_not_a_real_key_either"},
        {},
    ],
    ids=["unknown-key", "unknown-bearer", "missing"],
)
async def test_every_rejection_looks_the_same(make_client, headers: dict[str, str]) -> None:
    """A rejected credential never says why beyond "not accepted".

    The transport (header vs bearer) and the shape of the value must not change
    the answer: if "no such key" were distinguishable from "malformed key", the
    endpoint would be a key oracle that an attacker can enumerate against. Each
    case is a 401 with the same code, no `key_prefix` echo, and no hint about
    what a valid key looks like.
    """
    client = await make_client(api_key_enforcement_enabled=True)

    response = await client.get(PROTECTED, headers=headers)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] in {"API_KEY_MISSING", "API_KEY_INVALID"}
    assert "tb_" not in error["message"]
    assert "not_a_real_key" not in response.text


async def test_a_valid_key_is_accepted_over_both_transports(make_client, session) -> None:
    client = await make_client(api_key_enforcement_enabled=True)
    raw = await issue_key(client, session, scopes=["read:tenders"])

    header = await client.get(PROTECTED, headers={"X-API-Key": raw})
    bearer = await client.get(PROTECTED, headers={"Authorization": f"Bearer {raw}"})

    assert header.status_code == bearer.status_code == 200
    assert header.json()["meta"]["request_id"]


async def test_expired_and_revoked_keys_are_refused(make_client, session) -> None:
    client = await make_client(api_key_enforcement_enabled=True)
    expired = await issue_key(client, session, expire_in=timedelta(minutes=-1))
    revoked = await issue_key(client, session, revoke=True)
    valid = await issue_key(client, session, expire_in=timedelta(hours=1))

    for raw in (expired, revoked):
        response = await client.get(PROTECTED, headers={"X-API-Key": raw})
        assert response.status_code == 401, raw[:12]
    # ...and the same call still works with an unexpired key, so the two 401s
    # above are about the credential rather than the environment.
    assert (await client.get(PROTECTED, headers={"X-API-Key": valid})).status_code == 200


async def test_a_key_without_the_scope_is_refused_but_not_everywhere(make_client, session) -> None:
    """Scope enforcement is per endpoint family, not global.

    ``read:statistics`` must open ``/statistics`` and must not open
    ``/tenders``; the 403 says which scope is required so an integrator can fix
    their key instead of guessing.
    """
    client = await make_client(api_key_enforcement_enabled=True)
    raw = await issue_key(client, session, scopes=["read:statistics"])

    allowed = await client.get("/api/v1/statistics", headers={"X-API-Key": raw})
    refused = await client.get(PROTECTED, headers={"X-API-Key": raw})

    assert allowed.status_code == 200
    assert refused.status_code == 403
    error = refused.json()["error"]
    assert error["code"] == "INSUFFICIENT_SCOPE"
    assert "read:tenders" in error["message"] + str(error.get("details"))


async def test_nested_routes_inherit_their_collections_scope(make_client, session) -> None:
    """``/tenders/{id}/documents`` must not be reachable with only documents read."""
    client = await make_client(api_key_enforcement_enabled=True)
    raw = await issue_key(client, session, scopes=["read:documents"])

    response = await client.get(f"/api/v1/tenders/{uuid4()}/documents", headers={"X-API-Key": raw})

    assert response.status_code == 403


# -- the api-key endpoints themselves ---------------------------------------


async def test_key_management_requires_admin(make_client, session) -> None:
    client = await make_client(api_key_enforcement_enabled=True, api_key_self_service_enabled=True)
    reader = await issue_key(client, session, scopes=["read:tenders"])
    admin = await issue_key(client, session, scopes=["admin"])

    anonymous = await client.get("/api/v1/api-keys")
    as_reader = await client.get("/api/v1/api-keys", headers={"X-API-Key": reader})
    as_admin = await client.get("/api/v1/api-keys", headers={"X-API-Key": admin})

    assert anonymous.status_code == 401
    assert as_reader.status_code == 403
    assert as_admin.status_code == 200


async def test_minting_a_key_through_the_api_returns_the_secret_once(make_client, session) -> None:
    client = await make_client(
        api_key_enforcement_enabled=True,
        api_key_self_service_enabled=True,
    )
    admin = await issue_key(client, session, scopes=["admin"])

    created = await client.post(
        "/api/v1/api-keys",
        headers={"X-API-Key": admin},
        json={"name": "integration test key", "scopes": ["read:tenders"]},
    )

    assert created.status_code == 201, created.text
    body = created.json()
    raw = body["data"]["key"]
    assert raw and body["meta"] is not None
    # The secret is not retrievable a second time, so the response must not be
    # cacheable by a proxy or a browser.
    assert created.headers["cache-control"] == "no-store"

    listed = await client.get("/api/v1/api-keys", headers={"X-API-Key": admin})
    assert listed.status_code == 200
    assert raw not in listed.text
    assert "key_hash" not in listed.text

    usable = await client.get(PROTECTED, headers={"X-API-Key": raw})
    assert usable.status_code == 200


async def test_key_minting_over_the_api_is_off_by_default(make_client, session) -> None:
    """Self-service minting must stay an operator action unless enabled.

    A data API that hands out its own credentials turns any leak of an admin key
    into unbounded credential issuance.
    """
    client = await make_client(api_key_enforcement_enabled=True)
    admin = await issue_key(client, session, scopes=["admin"])

    refused = await client.post(
        "/api/v1/api-keys",
        headers={"X-API-Key": admin},
        json={"name": "should be refused", "scopes": ["read:tenders"]},
    )

    assert refused.status_code == 403
    assert "manage_api_keys" in refused.json()["error"]["message"]


async def test_revoked_key_cannot_be_reused(make_client, session) -> None:
    client = await make_client(
        api_key_enforcement_enabled=True,
        api_key_self_service_enabled=True,
    )
    admin = await issue_key(client, session, scopes=["admin"])
    created = await client.post(
        "/api/v1/api-keys",
        headers={"X-API-Key": admin},
        json={"name": "revoke me", "scopes": ["read:tenders"]},
    )
    raw = created.json()["data"]["key"]
    key_id = created.json()["data"]["id"]
    assert (await client.get(PROTECTED, headers={"X-API-Key": raw})).status_code == 200

    revoked = await client.post(
        f"/api/v1/api-keys/{key_id}/revoke",
        headers={"X-API-Key": admin},
        json={"reason": "rotation drill"},
    )
    after = await client.get(PROTECTED, headers={"X-API-Key": raw})

    assert revoked.status_code == 200, revoked.text
    assert after.status_code == 401


# -- rate limiting ----------------------------------------------------------


async def test_limits_are_enforced_with_retry_after_headers(make_client) -> None:
    client = await make_client(
        api_key_enforcement_enabled=False,
        rate_limit_enabled=True,
        rate_limit_anonymous_per_minute=1,
        rate_limit_burst=0,
        redis_url=DEAD_REDIS,
    )

    async with limiter_installed(settings_of(client)):
        first = await client.get(PROTECTED)
        second = await client.get(PROTECTED)

    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "1"
    assert first.headers["x-ratelimit-remaining"] == "0"
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert second.json()["error"]["details"]["limit_per_minute"] == 1


async def test_a_redis_outage_degrades_to_local_enforcement_and_says_so(
    make_client,
) -> None:
    """Losing Redis must not take the API down — but it must be visible.

    ``X-RateLimit-Policy`` names the backend so an operator (and a test) can
    tell global enforcement from per-replica enforcement instead of assuming the
    configured limits were applied cluster-wide.
    """
    client = await make_client(
        api_key_enforcement_enabled=False,
        rate_limit_enabled=True,
        redis_url=DEAD_REDIS,
    )

    async with limiter_installed(settings_of(client)):
        response = await client.get(PUBLIC)

    assert response.status_code == 200
    assert response.headers["x-ratelimit-policy"].startswith("in-process")


async def test_fail_closed_configuration_refuses_service_rather_than_guess(
    make_client,
) -> None:
    client = await make_client(
        api_key_enforcement_enabled=False,
        rate_limit_enabled=True,
        rate_limit_fail_open=False,
        redis_url=DEAD_REDIS,
    )

    async with limiter_installed(settings_of(client)):
        data = await client.get(PROTECTED)
        health = await client.get(PUBLIC)
        live = await client.get(f"{PUBLIC}/live")

    assert data.status_code == 503
    assert data.json()["error"]["code"] == "RATE_LIMIT_BACKEND_UNAVAILABLE"
    # Readiness agrees with serving behaviour: this configuration refuses data
    # requests without Redis, so it must not advertise itself as ready.
    assert health.status_code == 503
    cache = next(c for c in health.json()["components"] if c["name"] == "cache")
    assert cache["required"] is True and cache["status"] == "unhealthy"
    # Liveness stays green: it reports "the process is up", and restarting the
    # pod cannot fix a Redis outage — it only removes capacity mid-incident.
    assert live.status_code == 200


async def test_public_paths_are_limited_by_the_middleware_without_becoming_500(
    make_client,
) -> None:
    """A refusal raised inside middleware must still be a 429.

    The auth dependency and the public middleware both enforce limits, but they sit
    at different layers: an exception thrown by middleware escapes the application's
    exception handlers, so a *correctly limited* client was answered 500 "internal
    error" — the status a rate limit is specifically meant to avoid, and invisible to
    any alert keyed on 429. The middleware therefore returns the envelope itself.
    """
    client = await make_client(
        rate_limit_enabled=True,
        rate_limit_anonymous_per_minute=1,
        rate_limit_burst=0,
        redis_url=DEAD_REDIS,
    )

    async with limiter_installed(settings_of(client)):
        first = await client.get("/metrics")
        second = await client.get("/metrics")

    assert first.status_code == 200
    assert second.status_code == 429, second.text
    body = second.json()["error"]
    assert body["code"] == "RATE_LIMITED"
    assert int(second.headers["retry-after"]) == body["details"]["retry_after_seconds"]
    assert second.headers["x-ratelimit-remaining"] == "0"
    # The refusal still crosses the correlation/security layer on its way out: a
    # client quoting this 429 must be traceable to a log line, and security
    # headers are not negotiable per status code.
    assert second.headers["x-request-id"]
    assert second.headers["x-content-type-options"] == "nosniff"
    # And it is counted: an unauthenticated flood must be visible in the same
    # counter an operator would otherwise read as "no traffic".
    assert (await client.get("/metrics")).text.count("tenderbase_http_requests_total{") > 0


@pytest.mark.parametrize("enabled", [True, False], ids=["limiter-on", "limiter-off"])
async def test_an_authenticated_caller_is_limited_as_a_key_not_an_ip(
    make_client, session, enabled: bool
) -> None:
    """Distinct keys get distinct budgets; anonymous callers share one per IP."""
    client = await make_client(
        api_key_enforcement_enabled=enabled,
        rate_limit_enabled=True,
        rate_limit_anonymous_per_minute=1,
        rate_limit_burst=0,
        redis_url=DEAD_REDIS,
    )
    raw = await issue_key(client, session, scopes=["read:tenders"])
    headers = {"X-API-Key": raw} if enabled else {}

    async with limiter_installed(settings_of(client)):
        statuses = [(await client.get(PROTECTED, headers=headers)).status_code for _ in range(3)]

    if enabled:
        # The authenticated tier's own limit (60/min by default) is far above
        # three requests, so the anonymous limit must not be what is applied.
        assert statuses == [200, 200, 200]
    else:
        assert statuses == [200, 429, 429]


# -- metrics ----------------------------------------------------------------


async def test_metrics_are_exposed_as_prometheus_text(make_client) -> None:
    client = await make_client()

    awaited = await client.get(PUBLIC)
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    build_line = next(
        (line for line in body.splitlines() if line.startswith("tenderbase_build_info{")), ""
    )
    assert build_line, "the build Info metric must be published"
    # Labels are sorted alphabetically by the client, so match on content rather
    # than on a fixed order: the point is that the environment is reported and no
    # secret ever lands in these labels.
    assert 'environment="test"' in build_line
    assert "version=" in build_line
    assert "# TYPE tenderbase_http_requests_total counter" in body
    assert "tenderbase_http_requests_total{" in body
    # Pool saturation is exposed too (see app/observability/snapshot.py); the
    # series may be empty on a SQLite test engine, so the declaration is what is
    # asserted rather than a sample value.
    assert "# TYPE tenderbase_db_pool_connections gauge" in body
    # A gauge computed from the served database: empty in a fresh test database,
    # and proof the snapshot ran against *this* app's engine rather than failing.
    assert "tenderbase_tenders_total 0.0" in body
    assert awaited.status_code == 200


async def test_metrics_are_not_part_of_the_public_api_contract(make_client) -> None:
    client = await make_client()

    schema = (await client.get("/openapi.json")).json()

    assert "/metrics" not in schema["paths"]
    # ...while protected data endpoints do declare the security scheme, so
    # Swagger UI shows the padlock rather than inviting anonymous calls.
    schemes = schema["components"]["securitySchemes"]
    assert {"APIKeyHeader", "BearerAuth"} <= set(schemes)


async def test_metrics_can_require_a_scrape_token(make_client) -> None:
    client = await make_client(metrics_token="test-scrape-token")

    anonymous = await client.get("/metrics")
    wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    authorized = await client.get("/metrics", headers={"Authorization": "Bearer test-scrape-token"})

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 200
    # A scrape credential must never appear in a body or a log field.
    assert "test-scrape-token" not in anonymous.text


async def test_metrics_can_be_turned_off_entirely(make_client) -> None:
    client = await make_client(metrics_enabled=False)

    assert (await client.get("/metrics")).status_code == 404
