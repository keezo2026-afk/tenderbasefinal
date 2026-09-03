"""Unit tests for the normalization engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.connectors.base import RawItem, SourceContext
from app.enums import ConnectorType, OpportunityStatus, ProcurementType
from app.ingestion.normalizer import Normalizer
from app.schemas.document import DocumentCandidate


@pytest.fixture
def context() -> SourceContext:
    return SourceContext(
        id=str(uuid4()),
        name="TEST FIXTURE source",
        organization="Test Fixture Municipality",
        base_url="https://example.org",
        connector_type=ConnectorType.HTML,
    )


@pytest.fixture
def normalizer() -> Normalizer:
    return Normalizer()


def make_item(**fields) -> RawItem:
    return RawItem(source_url="https://example.org/tenders/1", fields=fields)


def test_normalizes_a_complete_item(normalizer, context):
    item = make_item(
        title="  Supply and delivery of solar panels ",
        reference_number="scm/2026/001",
        description="Full description of the requirement.",
        published_at="01 September 2026",
        closing_at="15 September 2026 at 11:00",
        estimated_value="R 1 250 000.00",
        procurement_type="RFQ",
        status="Open",
    )
    record = normalizer.normalize(item, context)

    assert record.title == "Supply and delivery of solar panels"
    assert record.reference_number == "scm/2026/001"
    assert record.reference_number_normalized == "SCM/2026/001"
    assert record.procurement_type is ProcurementType.RFQ
    assert record.status is OpportunityStatus.OPEN
    assert record.estimated_value == Decimal("1250000.00")
    assert record.currency == "ZAR"
    assert record.closing_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert record.content_hash and record.fingerprint
    assert record.source_timezone == "Africa/Johannesburg"


def test_missing_fields_become_null_not_invented(normalizer, context):
    record = normalizer.normalize(make_item(title="Cleaning services required"), context)
    assert record.reference_number is None
    assert record.published_at is None
    assert record.closing_at is None
    assert record.estimated_value is None
    assert record.currency is None
    assert record.contact is None


def test_raw_dates_are_preserved_for_audit(normalizer, context):
    record = normalizer.normalize(
        make_item(title="Supply of pumps", closing_at="closing date to be confirmed"), context
    )
    assert record.closing_at is None
    assert record.raw_dates["closing_at"]["raw"] == "closing date to be confirmed"


def test_procurement_type_inferred_from_title(normalizer, context):
    record = normalizer.normalize(
        make_item(title="Request for quotation: repair of water pumps"), context
    )
    assert record.procurement_type is ProcurementType.RFQ


def test_status_inferred_from_closing_date_when_absent(normalizer, context):
    past = normalizer.normalize(
        make_item(title="Old opportunity for services", closing_at="01 January 2020"), context
    )
    assert past.status is OpportunityStatus.CLOSED

    future = normalizer.normalize(
        make_item(title="Future opportunity for services", closing_at="01 January 2099"), context
    )
    assert future.status is OpportunityStatus.OPEN


def test_contact_extracted_from_free_text(normalizer, context):
    record = normalizer.normalize(
        make_item(
            title="Supply of equipment",
            description="Enquiries: scm@example.org or 031 555 0100.",
        ),
        context,
    )
    assert record.contact["email"] == "scm@example.org"
    assert record.contact["phone"].endswith("5550100")


def test_briefing_details_are_detected(normalizer, context):
    record = normalizer.normalize(
        make_item(
            title="Construction works",
            briefing_date="08 September 2026 at 10:00",
            briefing_location="Compulsory briefing at the Council Chamber",
        ),
        context,
    )
    assert record.briefing_required is True
    assert record.briefing_compulsory is True
    assert record.briefing_date is not None


def test_invalid_urls_are_dropped_rather_than_stored(normalizer, context):
    record = normalizer.normalize(
        make_item(title="Supply of goods", submission_url="javascript:alert(1)"), context
    )
    assert record.submission_url is None


def test_blank_title_is_rejected(normalizer, context):
    with pytest.raises(ValueError):
        normalizer.normalize(make_item(title="   "), context)


def test_documents_carry_through(normalizer, context):
    item = make_item(title="Supply of goods")
    item.documents = [DocumentCandidate(source_url="https://example.org/a.pdf", filename="a.pdf")]
    record = normalizer.normalize(item, context)
    assert len(record.documents) == 1
    assert "https://example.org/a.pdf" in record.hashable_payload()["documents"]


def test_identical_items_hash_identically(normalizer, context):
    fields = {"title": "Supply of goods", "closing_at": "15 September 2026"}
    first = normalizer.normalize(make_item(**fields), context)
    second = normalizer.normalize(make_item(**fields), context)
    assert first.content_hash == second.content_hash
    assert first.fingerprint == second.fingerprint
