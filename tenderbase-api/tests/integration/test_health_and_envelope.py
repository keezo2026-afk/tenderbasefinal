"""Integration tests for the API envelope, health probes and error handling."""

from __future__ import annotations

import pytest


async def test_root_advertises_the_api(client):
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["documentation"] == "/api/docs"
    assert body["openapi"] == "/openapi.json"
    assert body["health"] == "/api/v1/health"


async def test_health_reports_database_connectivity(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert any(c["name"] == "database" and c["status"] == "healthy" for c in body["components"])


async def test_liveness_does_no_io(client):
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


async def test_readiness_probe(client):
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    database = next(c for c in body["components"] if c["name"] == "database")
    assert database["status"] == "healthy"
    assert database["required"] is True


async def test_health_probes_are_also_mounted_at_the_root(client):
    """Orchestrators and the container HEALTHCHECK probe ``/health/live``.

    The versioned paths stay canonical; the root ones exist so an
    infrastructure component that cannot be configured with a prefix still
    works. Both must return the same shape.
    """
    def verdict(body: dict) -> tuple:
        # Timestamps and latencies obviously differ between two calls; the
        # verdict and its components must not.
        return (
            body["status"],
            tuple((c["name"], c["status"], c["required"]) for c in body["components"]),
        )

    for root, versioned in [
        ("/health", "/api/v1/health"),
        ("/health/live", "/api/v1/health/live"),
        ("/health/ready", "/api/v1/health/ready"),
    ]:
        at_root = await client.get(root)
        canonical = await client.get(versioned)
        assert at_root.status_code == 200, root
        assert verdict(at_root.json()) == verdict(canonical.json()), root


async def test_optional_cache_component_does_not_block_traffic(client):
    """With rate limiting off, Redis is not required — and not even probed."""
    body = (await client.get("/api/v1/health")).json()
    cache = next(c for c in body["components"] if c["name"] == "cache")
    assert cache["required"] is False
    assert cache["status"] == "disabled"
    assert body["status"] == "healthy"


async def test_redis_blocks_readiness_only_when_the_limiter_cannot_degrade(make_client):
    """Fail-open means "degrade visibly"; fail-closed means "refuse to serve".

    Readiness has to agree with whichever the operator chose. A limiter that has
    fallen back to in-process enforcement is still serving traffic correctly (and
    says so in ``X-RateLimit-Policy``), so blocking readiness would pull healthy
    nodes out of rotation — and have them restarted — during a Redis incident.
    With ``RATE_LIMIT_FAIL_OPEN=false`` the opposite holds: protected requests are
    answered 503, so readiness must fail or the load balancer keeps sending
    traffic to a node that refuses it.
    """
    dead = "redis://127.0.0.1:1/0"

    degrading = await make_client(rate_limit_enabled=True, redis_url=dead)
    ready = await degrading.get("/api/v1/health/ready")
    health = await degrading.get("/api/v1/health")

    assert ready.status_code == 200
    cache = next(c for c in ready.json()["components"] if c["name"] == "cache")
    assert cache["required"] is False
    # Reported as degraded rather than hidden: the outage is visible to
    # monitoring even though it does not block traffic.
    assert cache["status"] == "degraded"
    assert health.json()["status"] == "degraded"

    refusing = await make_client(
        rate_limit_enabled=True, redis_url=dead, rate_limit_fail_open=False
    )
    blocked = await refusing.get("/api/v1/health/ready")

    assert blocked.status_code == 503
    cache = next(c for c in blocked.json()["components"] if c["name"] == "cache")
    assert cache["required"] is True
    assert cache["status"] == "unhealthy"
    # Liveness deliberately ignores dependencies: a pod whose cache is down must
    # not be killed and restarted in a loop.
    assert (await refusing.get("/api/v1/health/live")).status_code == 200


async def test_request_id_is_echoed_and_generated(client):
    generated = await client.get("/api/v1/health/live")
    assert generated.headers["x-request-id"]

    supplied = await client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
    assert supplied.headers["x-request-id"] == "abc-123"


async def test_list_envelope_shape(client, make_opportunity):
    await make_opportunity()
    body = (await client.get("/api/v1/tenders")).json()
    assert set(body) == {"data", "pagination", "meta"}
    assert set(body["pagination"]) >= {
        "page",
        "page_size",
        "total_items",
        "total_pages",
        "has_next",
        "has_previous",
    }
    assert body["meta"]["request_id"]


async def test_unknown_route_returns_the_error_envelope(client):
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_validation_errors_are_structured(client):
    response = await client.get("/api/v1/tenders", params={"page": 0})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]


async def test_unknown_uuid_returns_404_not_500(client):
    response = await client.get("/api/v1/tenders/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"].endswith("NOT_FOUND")


async def test_malformed_uuid_returns_422(client):
    response = await client.get("/api/v1/tenders/not-a-uuid")
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/openapi.json", "/api/docs", "/api/redoc"])
async def test_documentation_endpoints_are_served(client, path):
    response = await client.get(path)
    assert response.status_code == 200


async def test_openapi_describes_every_v1_route(client):
    schema = (await client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    for expected in [
        "/api/v1/health",
        "/api/v1/tenders",
        "/api/v1/tenders/{tender_id}",
        "/api/v1/search",
        "/api/v1/municipalities",
        "/api/v1/provinces",
        "/api/v1/sources",
        "/api/v1/documents",
        "/api/v1/categories",
        "/api/v1/statistics",
    ]:
        assert expected in paths


async def test_no_frontend_is_served(client):
    """The build is API-only: no HTML app, no static mount."""
    assert (await client.get("/index.html")).status_code == 404
    assert (await client.get("/static/app.js")).status_code == 404
