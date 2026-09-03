"""End-to-end ingestion tests: a fixture website in, canonical records out.

These exercise the real pipeline (discover → fetch → parse → validate →
normalize → deduplicate → version → persist) against an ``httpx.MockTransport``
serving saved fixtures. No live website is contacted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models.document import Document
from app.db.models.opportunity import (
    OpportunityEvent,
    OpportunityVersion,
    ProcurementOpportunity,
)
from app.db.models.source import SourceRun
from app.enums import EventType, HealthStatus, JobStatus
from app.ingestion.pipeline import IngestionPipeline


@pytest.fixture
def listing_routes(fixture_loader):
    return {
        "https://example.org/tenders": (200, fixture_loader("html_listing.html"), "text/html"),
    }


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def run_pipeline(session, source, fetcher) -> SourceRun:
    pipeline = IngestionPipeline(fetcher=fetcher)
    return await pipeline.run_source(session, source)


async def test_pipeline_persists_normalized_opportunities(
    session, source, mock_fetcher, listing_routes
):
    fetcher = mock_fetcher(listing_routes)
    run = await run_pipeline(session, source, fetcher)

    assert run.status == str(JobStatus.COMPLETED)
    assert run.items_found == 3
    assert run.items_created == 3
    assert run.items_failed == 0

    opportunities = (
        (
            await session.execute(
                select(ProcurementOpportunity).order_by(ProcurementOpportunity.reference_number)
            )
        )
        .scalars()
        .all()
    )
    assert len(opportunities) == 3

    first = opportunities[0]
    assert first.reference_number == "FIXTURE/SCM/2026/001"
    assert first.source_id == source.id
    assert first.municipality_id == source.municipality_id
    assert first.province_id == source.province_id
    assert first.content_hash and first.fingerprint
    assert first.closing_at is not None
    assert first.version == 1
    assert first.first_seen_at is not None and first.last_seen_at is not None
    # Nothing is invented: unavailable fields stay NULL.
    assert first.estimated_value is None
    await fetcher.aclose()


async def test_second_run_is_idempotent(session, source, mock_fetcher, listing_routes):
    fetcher = mock_fetcher(listing_routes)
    await run_pipeline(session, source, fetcher)
    second = await run_pipeline(session, source, fetcher)

    assert await count(session, ProcurementOpportunity) == 3
    assert second.items_created == 0
    assert second.items_skipped == 3  # identical content hash → no-op
    # Only the three creation snapshots exist; a no-op run adds no versions.
    assert await count(session, OpportunityVersion) == 3
    await fetcher.aclose()


async def test_changed_content_creates_a_version_and_events(
    session, source, mock_fetcher, fixture_loader
):
    original = fixture_loader("html_listing.html")
    fetcher = mock_fetcher({"https://example.org/tenders": (200, original, "text/html")})
    await run_pipeline(session, source, fetcher)
    await fetcher.aclose()

    # The municipality extends the closing date of the first advert.
    changed = original.replace("15 September 2026 at 11:00", "15 October 2026 at 11:00")
    fetcher = mock_fetcher({"https://example.org/tenders": (200, changed, "text/html")})
    run = await run_pipeline(session, source, fetcher)

    assert run.items_updated == 1
    assert await count(session, ProcurementOpportunity) == 3

    updated = (
        await session.execute(
            select(ProcurementOpportunity).where(
                ProcurementOpportunity.reference_number == "FIXTURE/SCM/2026/001"
            )
        )
    ).scalar_one()
    assert updated.version == 2
    assert updated.closing_at.month == 10

    versions = (
        (
            await session.execute(
                select(OpportunityVersion)
                .where(OpportunityVersion.opportunity_id == updated.id)
                .order_by(OpportunityVersion.version)
            )
        )
        .scalars()
        .all()
    )
    assert len(versions) == 2  # creation snapshot + the change
    assert "closing_at" in versions[-1].changed_fields

    events = (
        (
            await session.execute(
                select(OpportunityEvent).where(OpportunityEvent.opportunity_id == updated.id)
            )
        )
        .scalars()
        .all()
    )
    assert str(EventType.DEADLINE_CHANGED) in {event.event_type for event in events}
    await fetcher.aclose()


async def test_documents_are_recorded_as_links_before_download(
    session, source, mock_fetcher, listing_routes
):
    source.config = {**source.config, "document_selector": "td:nth-child(5) a"}
    await session.commit()

    fetcher = mock_fetcher(listing_routes)
    await run_pipeline(session, source, fetcher)

    documents = (await session.execute(select(Document))).scalars().all()
    assert documents
    for document in documents:
        assert document.source_url.startswith("https://example.org/")
        assert document.is_downloaded is False
        assert document.sha256 is None  # identity comes from content, not the URL
    await fetcher.aclose()


async def test_source_health_is_derived_from_real_runs(
    session, source, mock_fetcher, listing_routes
):
    assert source.health_status == str(HealthStatus.UNKNOWN)

    fetcher = mock_fetcher(listing_routes)
    await run_pipeline(session, source, fetcher)
    await session.refresh(source)
    assert source.health_status == str(HealthStatus.HEALTHY)
    assert source.consecutive_failures == 0
    assert source.last_success_at is not None
    await fetcher.aclose()


async def test_unreachable_source_degrades_instead_of_raising(session, source, mock_fetcher):
    fetcher = mock_fetcher({})  # every request 404s
    run = await run_pipeline(session, source, fetcher)

    assert run.status == str(JobStatus.FAILED)
    assert run.items_created == 0
    await session.refresh(source)
    assert source.health_status == str(HealthStatus.DEGRADED)
    assert source.consecutive_failures == 1
    assert source.last_failure_at is not None
    assert source.active is True  # backed off by the scheduler, never auto-disabled
    await fetcher.aclose()


async def test_ingested_records_are_visible_through_the_api(
    client, session, source, mock_fetcher, listing_routes
):
    fetcher = mock_fetcher(listing_routes)
    await run_pipeline(session, source, fetcher)

    body = (await client.get("/api/v1/tenders")).json()
    assert body["pagination"]["total_items"] == 3
    references = {item["reference_number"] for item in body["data"]}
    assert "FIXTURE/SCM/2026/001" in references

    runs = (await client.get(f"/api/v1/sources/{source.id}/runs")).json()
    assert runs["pagination"]["total_items"] == 1
    assert runs["data"][0]["items_found"] == 3
    await fetcher.aclose()
