"""Integration tests for the tender, search, geography and category endpoints."""

from __future__ import annotations

from datetime import timedelta

from app.enums import OpportunityStatus, ProcurementType
from app.utils.dates import utcnow


async def test_list_tenders_returns_only_real_records_by_default(client, make_opportunity):
    await make_opportunity(title="TEST FIXTURE: Real ingested record")
    await make_opportunity(title="TEST FIXTURE: Development fixture", is_fixture=True)

    body = (await client.get("/api/v1/tenders")).json()
    assert body["pagination"]["total_items"] == 1

    with_fixtures = (
        await client.get("/api/v1/tenders", params={"include_test_fixtures": True})
    ).json()
    assert with_fixtures["pagination"]["total_items"] == 2
    assert any(item["is_test_fixture"] for item in with_fixtures["data"])


async def test_pagination_is_deterministic_and_non_overlapping(client, make_opportunity):
    for index in range(7):
        await make_opportunity(reference=f"FIXTURE/PAGE/{index:03d}", published_days_ago=index)

    first = (await client.get("/api/v1/tenders", params={"page": 1, "page_size": 3})).json()
    second = (await client.get("/api/v1/tenders", params={"page": 2, "page_size": 3})).json()
    third = (await client.get("/api/v1/tenders", params={"page": 3, "page_size": 3})).json()

    assert first["pagination"]["total_pages"] == 3
    assert first["pagination"]["has_next"] is True
    assert third["pagination"]["has_next"] is False

    ids = [item["id"] for page in (first, second, third) for item in page["data"]]
    assert len(ids) == 7
    assert len(set(ids)) == 7  # no record appears on two pages


async def test_page_size_is_bounded_by_the_server(client):
    response = await client.get("/api/v1/tenders", params={"page_size": 5000})
    assert response.status_code == 422


async def test_filtering_by_type_status_and_dates(client, make_opportunity):
    await make_opportunity(reference="FIXTURE/F/1", procurement_type=ProcurementType.RFQ)
    await make_opportunity(
        reference="FIXTURE/F/2",
        procurement_type=ProcurementType.TENDER,
        status=OpportunityStatus.CLOSED,
        closing_in_days=-5,
    )

    rfq = (await client.get("/api/v1/tenders", params={"type": "RFQ"})).json()
    assert [item["reference_number"] for item in rfq["data"]] == ["FIXTURE/F/1"]

    closed = (await client.get("/api/v1/tenders", params={"status": "CLOSED"})).json()
    assert [item["reference_number"] for item in closed["data"]] == ["FIXTURE/F/2"]

    future = (utcnow() + timedelta(days=7)).date().isoformat()
    closing = (await client.get("/api/v1/tenders", params={"closing_before": future})).json()
    assert closing["pagination"]["total_items"] == 1


async def test_filtering_by_municipality_and_province(client, make_opportunity, municipality):
    await make_opportunity()
    hit = await client.get("/api/v1/tenders", params={"municipality": municipality.slug})
    assert hit.json()["pagination"]["total_items"] == 1

    # An unknown identifier is a valid filter that simply matches nothing.
    miss = await client.get("/api/v1/tenders", params={"municipality": "no-such-municipality"})
    assert miss.status_code == 200
    assert miss.json()["pagination"]["total_items"] == 0


async def test_sorting_by_closing_date(client, make_opportunity):
    await make_opportunity(reference="FIXTURE/S/LATE", closing_in_days=30)
    await make_opportunity(reference="FIXTURE/S/SOON", closing_in_days=2)

    ascending = (await client.get("/api/v1/tenders", params={"sort": "closing_at"})).json()
    assert [item["reference_number"] for item in ascending["data"]] == [
        "FIXTURE/S/SOON",
        "FIXTURE/S/LATE",
    ]


async def test_tender_detail_exposes_provenance_and_relations(client, make_opportunity):
    opportunity = await make_opportunity()
    body = (await client.get(f"/api/v1/tenders/{opportunity.id}")).json()["data"]

    assert body["source_url"].startswith("https://example.org/")
    assert body["content_hash"]
    assert body["data_quality"] == "VALID"
    assert body["documents"] == []
    assert body["categories"] == []
    assert "sqlalchemy" not in str(body).lower()


async def test_tender_subresources(client, make_opportunity):
    opportunity = await make_opportunity()
    for suffix in ("documents", "events", "versions"):
        response = await client.get(f"/api/v1/tenders/{opportunity.id}/{suffix}")
        assert response.status_code == 200, suffix
        assert response.json()["data"] == []

    missing = await client.get("/api/v1/tenders/00000000-0000-0000-0000-000000000000/documents")
    assert missing.status_code == 404


# --- search ---------------------------------------------------------------


async def test_search_matches_titles(client, make_opportunity):
    await make_opportunity(title="TEST FIXTURE: Supply of solar photovoltaic panels")
    await make_opportunity(
        reference="FIXTURE/SEARCH/2", title="TEST FIXTURE: Grass cutting services"
    )

    body = (await client.get("/api/v1/search", params={"q": "photovoltaic"})).json()
    assert body["pagination"]["total_items"] == 1
    hit = body["data"][0]
    assert "photovoltaic" in hit["title"].lower()
    assert hit["score"] is None or hit["score"] >= 0  # SQLite has no ts_rank


async def test_search_returns_empty_results_rather_than_errors(client, make_opportunity):
    await make_opportunity()
    body = (await client.get("/api/v1/search", params={"q": "zzzzzznomatch"})).json()
    assert body["data"] == []
    assert body["pagination"]["total_items"] == 0


async def test_search_requires_a_query(client):
    assert (await client.get("/api/v1/search")).status_code == 422
    assert (await client.get("/api/v1/search", params={"q": "a"})).status_code == 422


async def test_search_reports_the_backend_used(client, make_opportunity):
    await make_opportunity()
    body = (await client.get("/api/v1/search", params={"q": "solar"})).json()
    extra = body["meta"]["extra"]
    assert extra["query"] == "solar"
    assert extra["search_backend"]
    assert extra["took_ms"] >= 0


# --- geography ------------------------------------------------------------


async def test_provinces_endpoint(client, province, municipality):
    body = (await client.get("/api/v1/provinces")).json()
    assert body["pagination"]["total_items"] == 1
    assert body["data"][0]["code"] == "KZN"

    detail = (await client.get(f"/api/v1/provinces/{province.code}")).json()["data"]
    assert detail["slug"] == "kwazulu-natal"

    by_slug = (await client.get("/api/v1/provinces/kwazulu-natal")).json()["data"]
    assert by_slug["id"] == detail["id"]

    assert (await client.get("/api/v1/provinces/ZZ")).status_code == 404


async def test_districts_endpoint_is_empty_without_imported_data(client, province):
    body = (await client.get("/api/v1/provinces/districts")).json()
    assert body["data"] == []


async def test_municipalities_endpoint(client, municipality):
    body = (await client.get("/api/v1/municipalities")).json()
    assert body["data"][0]["code"] == "ZZTEST"

    filtered = (
        await client.get("/api/v1/municipalities", params={"province": "KZN", "type": "LOCAL"})
    ).json()
    assert filtered["pagination"]["total_items"] == 1

    detail = (await client.get(f"/api/v1/municipalities/{municipality.code}")).json()["data"]
    assert detail["name"] == "Test Fixture Municipality"
    assert (await client.get("/api/v1/municipalities/ZZUNKNOWN")).status_code == 404


async def test_municipality_tenders_endpoint(client, municipality, make_opportunity):
    await make_opportunity()
    body = (await client.get(f"/api/v1/municipalities/{municipality.slug}/tenders")).json()
    assert body["pagination"]["total_items"] == 1


# --- categories, events, statistics --------------------------------------


async def test_categories_endpoint_is_empty_until_seeded(client):
    body = (await client.get("/api/v1/categories")).json()
    assert body["data"] == []


async def test_events_feed(client, make_opportunity):
    await make_opportunity()
    body = (await client.get("/api/v1/events")).json()
    assert body["data"] == []


async def test_statistics_are_computed_not_invented(client, make_opportunity):
    body = (await client.get("/api/v1/statistics")).json()["data"]
    assert body["total_opportunities"] == 0

    await make_opportunity()
    await make_opportunity(reference="FIXTURE/STATS/DEV", is_fixture=True)
    body = (await client.get("/api/v1/statistics")).json()["data"]
    assert body["total_opportunities"] == 1
    assert body["open_opportunities"] == 1
    assert body["test_fixture_opportunities"] == 1
    assert body["total_documents"] == 0
    assert body["total_municipalities"] == 1
    assert body["generated_at"]
