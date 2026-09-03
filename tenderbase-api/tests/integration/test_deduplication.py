"""Integration tests for layered deduplication against a real session."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.enums import DuplicateDecision, OpportunityStatus, ProcurementType
from app.ingestion.deduplicator import Deduplicator
from app.schemas.tender import NormalizedOpportunity
from app.utils.hashing import content_hash, fingerprint


def build_record(opportunity, **overrides) -> NormalizedOpportunity:
    payload = {
        "reference_number": opportunity.reference_number,
        "title": opportunity.title,
        "organization": opportunity.organization,
        "closing_at": opportunity.closing_at.isoformat() if opportunity.closing_at else None,
        "procurement_type": opportunity.procurement_type,
    }
    payload.update({k: v for k, v in overrides.items() if k in payload})
    data = dict(
        title=overrides.get("title", opportunity.title),
        reference_number=overrides.get("reference_number", opportunity.reference_number),
        reference_number_normalized=overrides.get(
            "reference_number_normalized", opportunity.reference_number_normalized
        ),
        organization=overrides.get("organization", opportunity.organization),
        municipality_id=overrides.get("municipality_id", opportunity.municipality_id),
        province_id=opportunity.province_id,
        source_id=overrides.get("source_id", opportunity.source_id),
        source_url=overrides.get("source_url", opportunity.source_url),
        closing_at=overrides.get("closing_at", opportunity.closing_at),
        published_at=opportunity.published_at,
        procurement_type=ProcurementType.parse(opportunity.procurement_type),
        status=OpportunityStatus.parse(opportunity.status),
        content_hash=overrides.get("content_hash", content_hash(payload)),
        fingerprint=overrides.get("fingerprint", fingerprint(payload)),
    )
    return NormalizedOpportunity(**data)


async def test_layer1_matches_municipality_and_reference(session, make_opportunity):
    existing = await make_opportunity(reference="FIXTURE/DEDUP/001")
    # Same issuer + reference, but the title was edited on the website.
    record = build_record(existing, title="TEST FIXTURE: edited title", content_hash="0" * 64)

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.EXACT_MATCH
    assert result.existing_id == existing.id
    assert result.layer == "reference_number"
    assert result.is_duplicate is True


async def test_layer2_matches_identical_content_hash(session, make_opportunity):
    existing = await make_opportunity(reference="FIXTURE/DEDUP/002")
    record = build_record(existing, reference_number=None, reference_number_normalized=None)
    record.content_hash = existing.content_hash

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.EXACT_MATCH
    assert result.layer == "content_hash"


async def test_layer3_matches_field_fingerprint(session, make_opportunity):
    existing = await make_opportunity(reference="FIXTURE/DEDUP/003")
    record = build_record(existing, reference_number=None, reference_number_normalized=None)
    record.content_hash = "1" * 64
    record.fingerprint = existing.fingerprint

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision in {
        DuplicateDecision.EXACT_MATCH,
        DuplicateDecision.PROBABLE_MATCH,
    }
    assert result.layer == "fingerprint"
    assert result.existing_id == existing.id


async def test_distinct_records_are_not_duplicates(session, make_opportunity):
    await make_opportunity(reference="FIXTURE/DEDUP/004")
    other = await make_opportunity(
        reference="FIXTURE/DEDUP/005", title="TEST FIXTURE: completely different subject"
    )
    record = build_record(other, reference_number="FIXTURE/DEDUP/006")
    record.reference_number_normalized = "FIXTURE/DEDUP/006"
    record.content_hash = "2" * 64
    record.fingerprint = "3" * 64

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.NEW
    assert result.existing_id is None


async def test_same_reference_in_a_different_municipality_is_not_a_duplicate(
    session, make_opportunity, province
):
    from app.db.models.geography import Municipality
    from app.enums import MunicipalityType

    existing = await make_opportunity(reference="SCM/2026/001")
    other_municipality = Municipality(
        name="Second Test Fixture Municipality",
        code="ZZTEST2",
        slug="second-test-fixture-municipality",
        type=str(MunicipalityType.LOCAL),
        province_id=province.id,
        data_source="TEST FIXTURE",
    )
    session.add(other_municipality)
    await session.commit()

    record = build_record(existing, municipality_id=other_municipality.id)
    record.content_hash = "4" * 64
    record.fingerprint = "5" * 64

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.NEW


async def test_fuzzy_layer_degrades_on_sqlite_and_holds_on_postgres(
    session, make_opportunity, require_postgres
):
    """Near-identical titles with no reference number are **never** auto-merged.

    On SQLite the trigram layer is unavailable and the record is treated as new
    (a silent, documented degradation). On PostgreSQL the layer runs and must
    return ``UNCERTAIN`` — review, not merge — because a similar title alone is
    not proof that this is the same procurement. The contract that matters in
    both dialects is: nothing gets merged on text similarity below the probable
    threshold.
    """
    existing = await make_opportunity(
        reference="FIXTURE/DEDUP/007", title="TEST FIXTURE: Supply of office furniture"
    )
    record = build_record(
        existing,
        reference_number=None,
        title="TEST FIXTURE: Supply of office furniture (re-advertised)",
        closing_at=existing.closing_at + timedelta(hours=2),
    )
    record.reference_number_normalized = None
    record.content_hash = "6" * 64
    record.fingerprint = "7" * 64

    result = await Deduplicator().find_duplicate(session, record)
    if require_postgres(skip=False):
        assert result.decision is DuplicateDecision.UNCERTAIN
        assert result.layer == "trigram"
        assert result.confidence < 0.82
        assert result.is_duplicate is False
        assert result.existing_id == existing.id
    else:
        assert result.decision is DuplicateDecision.NEW


async def test_conflicting_reference_numbers_are_never_merged_by_similarity(
    session, make_opportunity, require_postgres
):
    """A re-advertisement keeps the same *title* but publishes a new reference.

    Similarity must lose to that explicit signal in both dialects, otherwise a
    re-advertised tender would be swallowed into the original's history and the
    new publication would be invisible.
    """
    original = await make_opportunity(
        reference="RFQ/2026/001",
        title="TEST FIXTURE: Appointment of a service provider for road patch repairs",
    )
    # Identical title, *newly published* reference number: a re-advertisement.
    record = build_record(
        original,
        reference_number="RFQ/2026/088",
        reference_number_normalized="RFQ/2026/088",
        title="TEST FIXTURE: Appointment of a service provider for road patch repairs",
        source_url="https://example.org/tenders/RFQ-2026-088",
    )
    record.content_hash = "8" * 64
    record.fingerprint = "9" * 64

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.NEW
    assert result.existing_id is None


async def test_identical_record_is_a_content_hash_duplicate(session, make_opportunity):
    """Re-ingesting the byte-identical listing row must not create a new record."""
    existing = await make_opportunity(reference="FIXTURE/DEDUP/010")
    record = build_record(existing, reference_number=None, reference_number_normalized=None)
    record.content_hash = existing.content_hash
    record.fingerprint = "f" * 64  # force layer 2, not layer 3

    result = await Deduplicator().find_duplicate(session, record)
    assert result.decision is DuplicateDecision.EXACT_MATCH
    assert result.layer == "content_hash"
    assert result.existing_id == existing.id


@pytest.mark.parametrize("decision", list(DuplicateDecision))
def test_only_exact_and_probable_count_as_duplicates(decision):
    from app.ingestion.deduplicator import DedupResult

    result = DedupResult(decision=decision)
    assert result.is_duplicate is (
        decision in {DuplicateDecision.EXACT_MATCH, DuplicateDecision.PROBABLE_MATCH}
    )
    assert DuplicateDecision.UNCERTAIN not in {
        DuplicateDecision.EXACT_MATCH,
        DuplicateDecision.PROBABLE_MATCH,
    }  # uncertain matches are never merged
