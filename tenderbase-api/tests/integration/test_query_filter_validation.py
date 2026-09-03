"""Invalid query filters are the client's fault, and must be reported as such.

The list endpoints build their filter model inside a dependency, which is outside the
path FastAPI uses to turn bad input into a 422. A typo in a status, a reversed date
range or a negative value was therefore served as **500 INTERNAL_ERROR** — a client
mistake dressed up as a server fault, with no indication of which parameter was wrong.
`app.api.query_filters.parse_query_filter` is the seam; these tests pin the contract it
exists to provide.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _entry_for(payload: dict, field: str) -> dict:
    """The reported error for one parameter, failing loudly if it is not named."""
    entries = payload["error"]["details"]["errors"]
    wanted = f"query.{field}"
    for entry in entries:
        if entry["field"] == wanted:
            return entry
    raise AssertionError(f"{wanted} not in {[e['field'] for e in entries]}")


def _error_fields(payload: dict) -> list[str]:
    errors = payload["error"]["details"]["errors"]
    return [entry["field"] for entry in errors]


def assert_validation_error(response, *, field: str, fragment: str) -> None:
    assert response.status_code == 422, response.text
    payload = response.json()
    assert set(payload) == {"error"}
    error = payload["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"]
    entry = _entry_for(payload, field)
    # The word may live in either half: "enum" is the error *type*, while the message
    # spells out the allowed values.
    assert fragment.lower() in f"{entry['message']} {entry['type']}".lower(), entry


@pytest.mark.parametrize(
    ("path", "field", "fragment"),
    [
        ("/api/v1/tenders?status=NOT_A_STATUS", "status", "enum"),
        ("/api/v1/tenders?type=SOLAR_SYSTEM", "type", "enum"),
        ("/api/v1/tenders?data_quality=PERFECT", "data_quality", "enum"),
        ("/api/v1/municipalities?type=SOLAR_SYSTEM", "type", "enum"),
        ("/api/v1/sources?health_status=EXPLODING", "health_status", "enum"),
        ("/api/v1/sources?connector_type=CRYPTO", "connector_type", "enum"),
        ("/api/v1/tenders?published_after=nonsense", "published_after.date", "date"),
        ("/api/v1/tenders?min_value=-5", "min_value", "greater than"),
    ],
)
async def test_bad_filter_values_are_422_not_500(client, path, field, fragment):
    """Every rejection names the offending parameter instead of blaming the server."""
    assert_validation_error(await client.get(path), field=field, fragment=fragment)


async def test_reversed_date_range_is_reported_as_a_validation_error(client):
    """Cross-field checks live on the model, so they surface with no field to blame.

    The entry is keyed ``query`` rather than a parameter name: `published_after` is
    each individually fine, and pointing at one of them would be misleading.
    """
    response = await client.get(
        "/api/v1/tenders?published_after=2026-09-10&published_before=2026-09-01"
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert _error_fields(response.json()) == ["query"]
    assert (
        "published_after must be earlier than published_before"
        in error["details"]["errors"][0]["message"]
    )


async def test_reversed_value_range_is_reported(client):
    response = await client.get("/api/v1/tenders?min_value=1000&max_value=10")
    assert response.status_code == 422, response.text
    entry = response.json()["error"]["details"]["errors"][0]
    assert entry["field"] == "query" or entry["field"] == "query.min_value"
    assert "min_value" in entry["message"] or "max_value" in entry["message"]


async def test_valid_filters_are_unaffected(client):
    """The guard must not have tightened what the API accepts."""
    response = await client.get(
        "/api/v1/tenders?status=OPEN&type=RFQ&published_after=2026-01-01"
        "&closing_before=2026-12-31T23:00:00Z&min_value=1&max_value=10.50"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["pagination"]["total_pages"] == 0


async def test_datetime_value_for_a_date_filter_still_parses(client):
    """Callers pass full timestamps; a filter typed as a date must keep accepting them.

    The offset has to arrive percent-encoded: in a query string ``+`` means a space, so
    an unencoded ``+02:00`` is not a timezone at all by the time it is parsed.
    """
    response = await client.get("/api/v1/tenders?published_after=2026-09-01T08%3A30%3A00%2B02%3A00")
    assert response.status_code == 200, response.text

    unencoded = await client.get("/api/v1/tenders?published_after=2026-09-01T08:30:00+02:00")
    assert unencoded.status_code == 422
    messages = " ".join(e["message"] for e in unencoded.json()["error"]["details"]["errors"])
    assert "date" in messages.lower()


async def test_no_validation_error_leaks_internal_details(client):
    """A rejected filter must not echo the model, the SQL or a stack trace."""
    response = await client.get("/api/v1/tenders?status=NOPE&data_quality=NOPE")
    assert response.status_code == 422
    body = response.text
    for marker in ("Traceback", "pydantic", "SELECT", "TenderFilter", "/app/"):
        assert marker not in body


class TestPaginationBounds:
    """`page_size` is bounded by MAX_PAGE_SIZE, and the bound is the one configured.

    A hard-coded ceiling in the query annotation made the setting a one-way knob:
    raising it changed nothing, and lowering it *silently* returned fewer rows than the
    client asked for — the `page_size` in the response then disagreed with the request.
    """

    async def test_default_ceiling_is_rejected_by_name(self, client):
        response = await client.get("/api/v1/tenders?page_size=101")
        assert_validation_error(response, field="page_size", fragment="less than or equal")

    async def test_raising_the_ceiling_takes_effect(self, make_client):
        roomy = await make_client(max_page_size=200)
        try:
            accepted = await roomy.get("/api/v1/tenders?page_size=150")
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["pagination"]["page_size"] == 150

            refused = await roomy.get("/api/v1/tenders?page_size=201")
            assert refused.status_code == 422
            assert "200" in refused.json()["error"]["message"]
        finally:
            await roomy.aclose()

    async def test_lowering_the_ceiling_rejects_rather_than_truncating(self, make_client):
        tight = await make_client(max_page_size=5)
        try:
            over = await tight.get("/api/v1/tenders?page_size=6")
            assert over.status_code == 422, over.text
            assert _entry_for(over.json(), "page_size")["message"].endswith("5")

            at_limit = await tight.get("/api/v1/tenders?page_size=5")
            assert at_limit.status_code == 200, at_limit.text
            assert at_limit.json()["pagination"]["page_size"] == 5

            default = await tight.get("/api/v1/tenders")
            assert default.json()["pagination"]["page_size"] == 5  # DEFAULT_PAGE_SIZE clamps to 5
        finally:
            await tight.aclose()

    async def test_page_number_bounds(self, client):
        assert (await client.get("/api/v1/tenders?page=0")).status_code == 422
        assert (await client.get("/api/v1/tenders?page=10001")).status_code == 422
        assert (await client.get("/api/v1/tenders?page=9999")).status_code == 200
