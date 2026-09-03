"""Integration tests for the source registry and document endpoints."""

from __future__ import annotations

from datetime import timedelta

from app.db.models.document import Document, DocumentText, DocumentVersion
from app.db.models.source import SourceRun
from app.enums import DocumentFormat, DocumentType, ExtractionMethod, JobStatus
from app.utils.dates import utcnow
from app.utils.hashing import sha256_bytes

# --- sources --------------------------------------------------------------


async def test_list_sources_exposes_registry_and_health(client, source):
    body = (await client.get("/api/v1/sources")).json()
    assert body["pagination"]["total_items"] == 1
    item = body["data"][0]
    assert item["slug"] == "test-fixture-source"
    assert item["base_url"] == "https://example.org"
    assert item["connector_key"] == "html.listing"
    # Health is reported as a nested object, never fabricated before a run.
    assert item["health"]["health_status"] == "UNKNOWN"
    assert item["health"]["last_success_at"] is None
    assert item["health"]["consecutive_failures"] == 0


async def test_sources_can_be_filtered(client, source):
    active = await client.get("/api/v1/sources", params={"active": True})
    assert active.json()["pagination"]["total_items"] == 1

    other_province = await client.get("/api/v1/sources", params={"province": "WC"})
    assert other_province.json()["pagination"]["total_items"] == 0


async def test_get_source_and_missing_source(client, source):
    detail = (await client.get(f"/api/v1/sources/{source.id}")).json()["data"]
    assert detail["id"] == str(source.id)
    assert detail["connector_type"] == "HTML"
    assert detail["robots_policy"] == "RESPECT"

    missing = await client.get("/api/v1/sources/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


async def test_connector_catalogue_is_served(client):
    body = (await client.get("/api/v1/sources/connectors")).json()
    keys = {item["key"] for item in body["data"]}
    assert {"html.listing", "wordpress.rest", "custom.etender_ocds"}.issubset(keys)


async def test_source_runs_are_listed_newest_first(client, session, source):
    now = utcnow()
    for index, offset in enumerate([2, 1, 0]):
        session.add(
            SourceRun(
                source_id=source.id,
                status=str(JobStatus.COMPLETED),
                started_at=now - timedelta(hours=offset),
                completed_at=now - timedelta(hours=offset) + timedelta(seconds=30),
                items_found=index,
            )
        )
    await session.commit()

    body = (await client.get(f"/api/v1/sources/{source.id}/runs")).json()
    assert body["pagination"]["total_items"] == 3
    assert [run["items_found"] for run in body["data"]] == [2, 1, 0]


# --- documents ------------------------------------------------------------


async def make_document(session, opportunity, *, downloaded: bool = True) -> Document:
    payload = b"%PDF-1.7 TEST FIXTURE document"
    digest = sha256_bytes(payload)
    document = Document(
        opportunity_id=opportunity.id,
        source_url="https://example.org/documents/fixture.pdf",
        filename="fixture-bid-document.pdf",
        document_type=str(DocumentType.TENDER_DOCUMENT),
        document_format=str(DocumentFormat.PDF),
        mime_type="application/pdf",
        is_downloaded=downloaded,
        sha256=digest if downloaded else None,
        file_size=len(payload) if downloaded else None,
        storage_key=f"documents/{digest[:2]}/{digest[2:4]}/{digest}.pdf" if downloaded else None,
        downloaded_at=utcnow() if downloaded else None,
    )
    session.add(document)
    await session.flush()
    if downloaded:
        session.add(
            DocumentVersion(
                document_id=document.id,
                version=1,
                sha256=digest,
                file_size=len(payload),
                mime_type="application/pdf",
                storage_key=document.storage_key,
                downloaded_at=utcnow(),
            )
        )
    await session.commit()
    return document


async def test_documents_listing_and_detail(client, session, make_opportunity):
    opportunity = await make_opportunity()
    document = await make_document(session, opportunity)

    listing = (await client.get("/api/v1/documents")).json()
    assert listing["pagination"]["total_items"] == 1
    assert listing["data"][0]["sha256"] == document.sha256

    detail = (await client.get(f"/api/v1/documents/{document.id}")).json()["data"]
    assert detail["document_type"] == "TENDER_DOCUMENT"
    assert detail["is_downloaded"] is True

    missing = await client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


async def test_document_versions_endpoint(client, session, make_opportunity):
    opportunity = await make_opportunity()
    document = await make_document(session, opportunity)

    body = (await client.get(f"/api/v1/documents/{document.id}/versions")).json()
    assert body["pagination"]["total_items"] == 1
    assert body["data"][0]["version"] == 1


async def test_document_text_endpoint_reports_extraction_provenance(
    client, session, make_opportunity
):
    opportunity = await make_opportunity()
    document = await make_document(session, opportunity)

    # Before extraction the endpoint 404s rather than returning invented text.
    assert (await client.get(f"/api/v1/documents/{document.id}/text")).status_code == 404

    session.add(
        DocumentText(
            document_id=document.id,
            content="TEST FIXTURE extracted body text",
            char_count=32,
            page_count=1,
            extraction_method=str(ExtractionMethod.NATIVE_PDF),
            ocr_used=False,
            extraction_confidence=0.99,
            extracted_at=utcnow(),
        )
    )
    await session.commit()

    body = (await client.get(f"/api/v1/documents/{document.id}/text")).json()["data"]
    assert body["extraction_method"] == "NATIVE_PDF"
    assert body["ocr_used"] is False
    assert body["content"].startswith("TEST FIXTURE")

    without = (
        await client.get(f"/api/v1/documents/{document.id}/text", params={"include_content": False})
    ).json()["data"]
    assert without["content"] is None
    assert without["char_count"] == 32


async def test_documents_can_be_filtered_by_opportunity(client, session, make_opportunity):
    first = await make_opportunity()
    second = await make_opportunity(reference="FIXTURE/DOCS/2")
    await make_document(session, first)

    body = (await client.get("/api/v1/documents", params={"opportunity_id": str(second.id)})).json()
    assert body["pagination"]["total_items"] == 0
