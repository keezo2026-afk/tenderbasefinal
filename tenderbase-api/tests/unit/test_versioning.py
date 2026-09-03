"""Unit tests for the version engine's diffing and event generation."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.enums import EventType, OpportunityStatus, ProcurementType
from app.ingestion.versioning import VersionEngine
from app.schemas.document import DocumentCandidate
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import utcnow


@pytest.fixture
def engine() -> VersionEngine:
    return VersionEngine()


def incoming(**overrides) -> NormalizedOpportunity:
    now = utcnow()
    payload = {
        "title": "Supply of solar panels",
        "description": "Description",
        "reference_number": "SCM/2026/001",
        "organization": "Test Fixture Municipality",
        "source_id": uuid4(),
        "source_url": "https://example.org/tenders/1",
        "published_at": now - timedelta(days=2),
        "closing_at": now + timedelta(days=14),
        "procurement_type": ProcurementType.RFQ,
        "status": OpportunityStatus.OPEN,
    }
    payload.update(overrides)
    return NormalizedOpportunity(**payload)


def existing_from(record: NormalizedOpportunity, **overrides):
    """A stand-in for the stored ORM row."""
    data = {
        "id": uuid4(),
        "reference_number": record.reference_number,
        "title": record.title,
        "description": record.description,
        "procurement_type": str(record.procurement_type),
        "status": str(record.status),
        "organization": record.organization,
        "published_at": record.published_at,
        "closing_at": record.closing_at,
        "estimated_value": None,
        "currency": None,
        "submission_method": None,
        "submission_url": None,
        "submission_address": None,
        "briefing_required": None,
        "briefing_compulsory": None,
        "briefing_date": None,
        "briefing_location": None,
        "source_url": record.source_url,
        "canonical_url": None,
        "documents": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_no_changes_produces_no_version(engine):
    record = incoming()
    decision = engine.diff(existing_from(record), record)
    assert not decision.changed
    assert decision.changed_fields == {}


def test_deadline_change_emits_deadline_event(engine):
    record = incoming()
    stored = existing_from(record, closing_at=record.closing_at - timedelta(days=7))
    decision = engine.diff(stored, record)
    assert decision.changed
    assert "closing_at" in decision.changed_fields
    assert any(event[0] is EventType.DEADLINE_CHANGED for event in decision.events)


def test_status_transitions_emit_specific_events(engine):
    record = incoming(status=OpportunityStatus.CANCELLED)
    stored = existing_from(record, status=str(OpportunityStatus.OPEN))
    decision = engine.diff(stored, record)
    assert any(event[0] is EventType.CANCELLED for event in decision.events)

    awarded = incoming(status=OpportunityStatus.AWARDED)
    decision = engine.diff(existing_from(awarded, status="OPEN"), awarded)
    assert any(event[0] is EventType.AWARD_POSTED for event in decision.events)


def test_added_documents_are_detected(engine):
    record = incoming()
    record.documents = [DocumentCandidate(source_url="https://example.org/new.pdf")]
    decision = engine.diff(existing_from(record), record)
    assert "documents_added" in decision.changed_fields
    assert any(event[0] is EventType.DOCUMENT_ADDED for event in decision.events)


def test_removed_documents_are_recorded_but_do_not_force_a_version(engine):
    record = incoming()
    stored = existing_from(
        record, documents=[SimpleNamespace(source_url="https://example.org/old.pdf")]
    )
    decision = engine.diff(stored, record)
    assert "documents_removed" in decision.changed_fields
    assert any(event[0] is EventType.DOCUMENT_REMOVED for event in decision.events)
    assert not decision.changed  # removal alone is informational


def test_null_incoming_values_never_erase_known_data(engine):
    record = incoming(description=None)
    stored = existing_from(record, description="A previously captured description")
    decision = engine.diff(stored, record)
    assert "description" not in decision.changed_fields


def test_snapshot_is_json_serialisable(engine):
    import json

    snapshot = engine.snapshot(incoming())
    json.dumps(snapshot)
    assert snapshot["title"] == "Supply of solar panels"
    assert snapshot["closing_at"].endswith("Z")
