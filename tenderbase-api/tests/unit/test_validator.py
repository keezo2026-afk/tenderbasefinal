"""Unit tests for the validation engine."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.enums import DataQuality, OpportunityStatus, ProcurementType
from app.ingestion.validator import Validator
from app.schemas.document import DocumentCandidate
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import utcnow


def build(**overrides) -> NormalizedOpportunity:
    now = utcnow()
    payload = {
        "title": "Supply and delivery of solar panels",
        "description": "A complete description of the requirement.",
        "reference_number": "SCM/2026/001",
        "organization": "Test Fixture Municipality",
        "source_id": uuid4(),
        "source_url": "https://example.org/tenders/1",
        "published_at": now - timedelta(days=2),
        "closing_at": now + timedelta(days=14),
        "procurement_type": ProcurementType.RFQ,
        "status": OpportunityStatus.OPEN,
        "documents": [DocumentCandidate(source_url="https://example.org/a.pdf")],
        "contact": {"email": "scm@example.org"},
    }
    payload.update(overrides)
    return NormalizedOpportunity(**payload)


@pytest.fixture
def validator() -> Validator:
    return Validator()


def test_complete_record_is_valid(validator):
    result = validator.validate(build())
    assert result.quality is DataQuality.VALID
    assert result.completeness == pytest.approx(1.0)
    assert result.is_persistable


def test_missing_optional_fields_yield_incomplete_not_invalid(validator):
    result = validator.validate(
        build(reference_number=None, description=None, documents=[], contact=None)
    )
    assert result.quality is DataQuality.INCOMPLETE
    assert result.is_persistable
    assert "reference_number" in result.issues


def test_blank_title_is_invalid(validator):
    record = build()
    record.title = ""
    result = validator.validate(record)
    assert result.quality is DataQuality.INVALID
    assert not result.is_persistable


def test_non_http_source_url_is_invalid(validator):
    record = build()
    record.source_url = "not-a-url"
    assert validator.validate(record).quality is DataQuality.INVALID


def test_closing_before_publication_needs_review(validator):
    now = utcnow()
    result = validator.validate(build(published_at=now, closing_at=now - timedelta(days=30)))
    assert result.quality is DataQuality.NEEDS_REVIEW
    assert "closing_at" in result.issues


def test_implausibly_distant_closing_date_needs_review(validator):
    result = validator.validate(build(closing_at=utcnow() + timedelta(days=365 * 25)))
    assert result.quality is DataQuality.NEEDS_REVIEW


def test_negative_value_needs_review(validator):
    result = validator.validate(build(estimated_value=Decimal("-1"), currency="ZAR"))
    assert result.quality is DataQuality.NEEDS_REVIEW
    assert "estimated_value" in result.issues


def test_unknown_type_and_status_are_recorded_as_issues(validator):
    result = validator.validate(
        build(procurement_type=ProcurementType.OTHER, status=OpportunityStatus.UNKNOWN)
    )
    assert "procurement_type" in result.issues
    assert "status" in result.issues
    assert result.is_persistable


def test_result_serialises_for_storage(validator):
    payload = validator.validate(build()).as_dict()
    assert set(payload) == {"quality", "completeness", "issues"}
