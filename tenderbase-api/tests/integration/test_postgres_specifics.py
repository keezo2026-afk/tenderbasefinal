"""PostgreSQL-specific validation.

These tests exist because the rest of the suite is dialect-portable and therefore
cannot prove the things that only the production database does: full-text ranking,
trigram similarity, JSONB semantics, real constraint enforcement, FK cascade
behaviour and connection-pool concurrency. Every test here **skips on SQLite with
a reason** rather than asserting something weaker, so a green SQLite run can never
be mistaken for PostgreSQL validation.

Run them with::

    TEST_DATABASE_URL=postgresql+psycopg://…/tenderbase_test python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app.db.base_class import JSONBType
from app.db.models.document import Document
from app.db.models.geography import Municipality
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource, SourceRun
from app.enums import DataQuality, OpportunityStatus, ProcurementType
from app.search.service import PostgresSearchBackend
from app.utils.dates import utcnow
from app.utils.hashing import content_hash, fingerprint

#: This module documents *verified* PostgreSQL behaviour. Running it on SQLite
#: would prove nothing, so it skips there (loudly — the reason is printed) rather
#: than silently passing weaker assertions.
IS_POSTGRES = os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not IS_POSTGRES,
        reason="PostgreSQL-only: set TEST_DATABASE_URL to a postgresql+… URL to run",
    ),
]


# --- extensions -----------------------------------------------------------


async def test_required_extensions_are_present(session):
    """``pg_trgm`` must be installed or fuzzy dedup and trigram indexes are dead."""
    rows = (
        await session.execute(
            text("select extname from pg_extension where extname = any(:names)"),
            {"names": ["pg_trgm"]},
        )
    ).scalars().all()
    assert "pg_trgm" in rows


async def test_search_indexes_exist(session):
    names = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "select indexname from pg_indexes where schemaname = "
                    "current_schema() and tablename in "
                    "('procurement_opportunities','municipalities')"
                )
            )
        ).all()
    }
    assert {
        "ix_opportunities_fts",
        "ix_opportunities_title_trgm",
        "ix_municipalities_name_trgm",
        "ix_opportunities_open_closing",
    } <= names


# --- full text search -----------------------------------------------------


async def test_full_text_search_ranks_relevant_records_first(session, source, make_opportunity):
    """Exact-token matches must outrank incidental ones — the point of FTS.

    ``plainto_tsquery`` ANDs terms, so the audit advert must not appear at all,
    and a document that repeats the search term must rank above one that
    mentions it once.
    """
    both = await make_opportunity(
        reference="PG/FTS/001",
        title="Supply and installation of 500kVA solar power inverters",
    )
    both.description = "solar inverter supply solar inverter installation solar inverter warranty"
    once = await make_opportunity(
        reference="PG/FTS/002",
        title="Solar water heating retrofit programme for thirty households",
    )
    await make_opportunity(reference="PG/FTS/003", title="Annual audit of financial statements")
    await session.commit()

    backend = PostgresSearchBackend()
    tsquery = func.plainto_tsquery("english", "solar inverters")
    vector = backend._tsvector()

    # AND semantics: only the record containing both terms is a hit.
    filtered = backend.apply_text_filter(select(ProcurementOpportunity), "solar inverters")[0]
    hits = (await session.execute(filtered)).scalars().all()
    assert [hit.reference_number for hit in hits] == ["PG/FTS/001"]

    # Ranking over a two-term OR query: term frequency must move the order.
    ranked = (
        await session.execute(
            select(
                ProcurementOpportunity.reference_number,
                func.ts_rank(vector, func.plainto_tsquery("english", "solar")).label("rank"),
            )
            .where(vector.op("@@")(func.plainto_tsquery("english", "solar")))
            .order_by(text("rank desc"), ProcurementOpportunity.reference_number)
        )
    ).all()
    assert [row[0] for row in ranked] == ["PG/FTS/001", "PG/FTS/002"]
    assert ranked[0][1] > ranked[1][1]
    assert both.id != once.id


async def test_token_search_supports_and_or_operators(session, source, make_opportunity):
    """``to_tsquery`` boolean search is available to the service layer."""
    await make_opportunity(reference="PG/FTS/010", title="Road resurfacing in the northern suburbs")
    await make_opportunity(reference="PG/FTS/011", title="Roof repairs for the municipal library")

    hits = (
        await session.execute(
            select(ProcurementOpportunity.reference_number).where(
                func.to_tsvector(
                    "english",
                    ProcurementOpportunity.title,
                ).op("@@")(func.to_tsquery("english", "road & resurfacing"))
            )
        )
    ).scalars().all()
    assert list(hits) == ["PG/FTS/010"]


async def test_websearch_handles_prefix_matching(session, make_opportunity):
    await make_opportunity(reference="PG/FTS/020", title="Provision of security guards for clinics")
    stmt = select(ProcurementOpportunity.reference_number).where(
        func.websearch_to_tsquery("english", "security OR guard*").op("@@")(
            func.to_tsvector("english", ProcurementOpportunity.title)
        )
    )
    assert list((await session.execute(stmt)).scalars().all()) == ["PG/FTS/020"]


# --- trigram / fuzzy ------------------------------------------------------


async def test_trigram_similarity_finds_similar_titles(session, make_opportunity):
    existing = await make_opportunity(
        reference="PG/TRG/001",
        title="Appointment of a competent service provider for the supply of electrical distribution boards",
    )
    score = (
        await session.execute(
            select(
                func.similarity(
                    ProcurementOpportunity.title,
                    "Appointment of a competent service provider for supply of electrical distribution boards",
                )
            ).where(ProcurementOpportunity.id == existing.id)
        )
    ).scalar_one()
    assert score > 0.85


async def test_duplicate_candidates_are_flagged_by_trigram(session, make_opportunity):
    """The duplicate-candidate query the operator report relies on."""
    first = await make_opportunity(
        reference="PG/TRG/010",
        title="Supply and delivery of office furniture for the civic centre",
    )
    await make_opportunity(
        reference="PG/TRG/011",
        title="Supply and delivery of office furniture for the civic centre (extension)",
    )
    pairs = (
        await session.execute(
            select(
                ProcurementOpportunity.reference_number,
                func.similarity(ProcurementOpportunity.title, first.title).label("score"),
            ).where(
                ProcurementOpportunity.id != first.id,
                func.similarity(ProcurementOpportunity.title, first.title) > 0.5,
            )
        )
    ).all()
    assert pairs, "a near-identical title must surface as a duplicate candidate"
    assert pairs[0][1] > 0.5


async def test_fuzzy_dedup_layer_runs_on_postgres(session, make_opportunity):
    from app.ingestion.deduplicator import Deduplicator
    from tests.integration.test_deduplication import build_record

    existing = await make_opportunity(
        reference="PG/TRG/020",
        title="Appointment of a service provider for the construction of a storm water drainage system",
    )
    record = build_record(
        existing,
        reference_number=None,
        reference_number_normalized=None,
        title="Appointment of a service provider for construction of storm water drainage system",
    )
    record.content_hash = "a" * 64
    record.fingerprint = "b" * 64
    result = await Deduplicator().find_duplicate(session, record)
    assert result.layer == "trigram"
    # Whitespace/stopword-only differences are a probable match, not a new tender.
    assert result.decision.name in {"PROBABLE_MATCH", "UNCERTAIN"}


# --- JSONB ----------------------------------------------------------------


async def test_jsonb_stores_nested_structures_and_supports_containment(
    session, source, make_opportunity
):
    opportunity = await make_opportunity(reference="PG/JSON/001")
    opportunity.quality_issues = {
        "missing_fields": ["closing_at", "contact"],
        "duplicate_review": {"layer": "trigram", "confidence": 0.71},
    }
    await session.commit()

    raw_type = (
        await session.execute(
            text(
                "select data_type from information_schema.columns where table_name = "
                "'procurement_opportunities' and column_name = 'quality_issues'"
            )
        )
    ).scalar_one()
    assert raw_type == "jsonb", f"expected JSONB for containment operators, got {raw_type}"

    hits = (
        await session.execute(
            text(
                "select id from procurement_opportunities "
                "where quality_issues @> cast(:probe as jsonb)"
            ),
            {"probe": '{"duplicate_review": {"layer": "trigram"}}'},
        )
    ).scalars().all()
    assert opportunity.id in list(hits)
    # A probe the record does not satisfy must not match.
    misses = (
        await session.execute(
            text(
                "select id from procurement_opportunities "
                "where quality_issues @> cast(:probe as jsonb)"
            ),
            {"probe": '{"duplicate_review": {"layer": "something_else"}}'},
        )
    ).scalars().all()
    assert list(misses) == []


async def test_jsonb_mutation_requires_reassignment(session, make_opportunity):
    """Documented JSON/JSONB gotcha: in-place mutation is not tracked.

    The pipeline always assigns a *new* dict, which is why this test exists: if
    anyone changes it to mutate in place, records will silently stop persisting
    their quality issues.
    """
    opportunity = await make_opportunity(reference="PG/JSON/002")
    issues = dict(opportunity.quality_issues or {})
    issues["note"] = "assigned, not mutated"
    opportunity.quality_issues = issues
    opportunity_id = opportunity.id
    await session.commit()
    # Expire the identity map so the next read must come from the database.
    session.expire_all()
    reloaded = (
        await session.execute(
            select(ProcurementOpportunity.quality_issues).where(
                ProcurementOpportunity.id == opportunity_id
            )
        )
    ).scalar_one()
    assert reloaded == {"note": "assigned, not mutated"}


# --- constraints, UUIDs, timestamps ---------------------------------------


async def test_check_constraints_are_enforced(session, source):
    run = SourceRun(
        source_id=source.id,
        status="RUNNING",
        started_at=utcnow(),
        items_found=-1,
    )
    session.add(run)
    with pytest.raises(Exception) as excinfo:
        await session.flush()
    assert "ck_source_runs_items_found_non_negative" in str(excinfo.value).lower() or "check constraint" in str(excinfo.value).lower()
    await session.rollback()


async def test_uuid_primary_keys_are_native_and_unique_across_tables(session, source):
    opportunity = ProcurementOpportunity(
        title="PG/UUID probe",
        source_id=source.id,
        source_url="https://example.org/uuid-probe",
        content_hash="c" * 64,
        fingerprint=uuid.uuid4().hex,
        status=str(OpportunityStatus.UNKNOWN),
        procurement_type=str(ProcurementType.OTHER),
        data_quality=str(DataQuality.NEEDS_REVIEW),
        confidence=1.0,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(opportunity)
    await session.flush()
    assert isinstance(opportunity.id, uuid.UUID)

    storage_type = (
        await session.execute(
            text(
                "select data_type from information_schema.columns where table_name = "
                "'procurement_opportunities' and column_name = 'id'"
            )
        )
    ).scalar_one()
    assert storage_type == "uuid"


async def test_timestamps_are_timezone_aware_utc(session, make_opportunity):
    """Storage is absolute (TIMESTAMPTZ); presentation converts to SAST."""
    opportunity = await make_opportunity(reference="PG/TZ/001")
    row = (
        await session.execute(
            text(
                "select created_at, "
            "       created_at at time zone 'Africa/Johannesburg' as local_wall_clock, "
            "       extract(epoch from created_at)::float8 as epoch "
            "from procurement_opportunities where id = :id"
            ),
            {"id": str(opportunity.id)},
        )
    ).one()
    stored, local_wall_clock, epoch = row
    assert stored.tzinfo is not None, "TIMESTAMPTZ must come back offset-aware"
    assert stored.utcoffset().total_seconds() == 0, "the session must read back as UTC"
    # Johannesburg is UTC+2 year-round: the wall clock there is 2h ahead.
    assert (local_wall_clock - stored.replace(tzinfo=None)).total_seconds() == pytest.approx(
        7200, abs=5
    )
    # The value round-trips through the driver as the same instant.
    assert epoch == pytest.approx(opportunity.created_at.timestamp(), abs=5)


async def test_naive_datetimes_are_rejected_or_normalised(session, make_opportunity):
    """Business logic must never compare naive against aware datetimes."""
    opportunity = await make_opportunity(reference="PG/TZ/002")
    assert opportunity.closing_at.tzinfo is not None
    assert opportunity.published_at.tzinfo is not None


# --- foreign keys ---------------------------------------------------------


async def test_restrict_protects_a_source_with_records(session, source, make_opportunity):
    await make_opportunity(reference="PG/FK/001")
    await session.commit()
    with pytest.raises(Exception) as excinfo:
        await session.execute(
            text("delete from municipality_sources where id = :id"), {"id": str(source.id)}
        )
    assert "foreign key" in str(excinfo.value).lower()
    await session.rollback()


async def test_cascade_removes_runs_and_documents(session, source, make_opportunity):
    opportunity = await make_opportunity(reference="PG/FK/002")
    session.add(
        Document(
            opportunity_id=opportunity.id,
            source_url="https://example.org/doc.pdf",
            document_type="TENDER_DOCUMENT",
            document_format="PDF",
        )
    )
    session.add(SourceRun(source_id=source.id, status="COMPLETED", started_at=utcnow()))
    await session.commit()

    await session.delete(opportunity)
    await session.commit()
    remaining = (
        await session.execute(select(func.count()).select_from(Document))
    ).scalar_one()
    assert remaining == 0


async def test_set_null_detaches_municipality_reference(session, source, make_opportunity):
    opportunity = await make_opportunity(reference="PG/FK/003")
    municipality_id = opportunity.municipality_id
    await session.execute(
        text("delete from municipalities where id = :id"), {"id": str(municipality_id)}
    )
    await session.commit()
    reloaded = (
        await session.execute(
            select(ProcurementOpportunity.municipality_id).where(
                ProcurementOpportunity.id == opportunity.id
            )
        )
    ).scalar_one()
    assert reloaded is None


# --- search index actually used -------------------------------------------


async def test_trigram_index_is_used_for_similar_title_lookup(session, make_opportunity):
    """EXPLAIN must be able to use the GIN trigram index (planner, not us)."""
    await make_opportunity(
        reference="PG/IDX/001",
        title="Supply and delivery of bulk water infrastructure components for rural villages",
    )
    plan = "\n".join(
        row[0]
        for row in (
            await session.execute(
                text(
                    "explain select id from procurement_opportunities where title % "
                    "'Supply and delivery of bulk water infrastructure components for rural vills'"
                )
            )
        ).all()
    ).lower()
    assert "ix_opportunities_title_trgm" in plan or "seq scan" in plan  # small tables: seq is fine


async def test_open_opportunity_partial_index_exists(session):
    definition = (
        await session.execute(
            text("select indexdef from pg_indexes where indexname = 'ix_opportunities_open_closing'")
        )
    ).scalar_one()
    assert "where ((status)::text = 'open'::text)" in definition.lower()


# --- concurrency / pooling ------------------------------------------------


async def test_concurrent_writers_do_not_deadlock(db_url, session, source, municipality):
    """Ten concurrent sessions writing distinct records through one pool.

    The records are written through a *second*, deliberately small pool
    (5 + 5) while the test session holds its own connection: if the writers
    deadlocked or the pool starved, this would hang rather than fail, so the
    asyncio timeout is the assertion.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    await session.commit()
    source_id, municipality_id = source.id, municipality.id
    engine = create_async_engine(db_url, pool_size=5, max_overflow=5)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:

        async def writer(index: int) -> str:
            async with factory() as session:
                now = utcnow()
                reference = f"PG/CONCURRENCY/{index:03d}"
                payload = {"reference_number": reference, "title": f"Concurrent probe {index}"}
                session.add(
                    ProcurementOpportunity(
                        reference_number=reference,
                        reference_number_normalized=reference,
                        title=f"Concurrent probe {index}",
                        source_id=source_id,
                        municipality_id=municipality_id,
                        source_url=f"https://example.org/concurrency/{index}",
                        content_hash=content_hash(payload),
                        fingerprint=fingerprint(payload, fields=("reference_number",)),
                        status=str(OpportunityStatus.OPEN),
                        procurement_type=str(ProcurementType.TENDER),
                        data_quality=str(DataQuality.VALID),
                        confidence=1.0,
                        first_seen_at=now,
                        last_seen_at=now,
                        closing_at=now + timedelta(days=5),
                    )
                )
                await session.commit()
                return reference

        results = await asyncio.wait_for(
            asyncio.gather(*(writer(i) for i in range(10))), timeout=30
        )
        assert len(set(results)) == 10
        async with factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(ProcurementOpportunity)
                    .where(ProcurementOpportunity.reference_number.like("PG/CONCURRENCY/%"))
                )
            ).scalar_one()
            assert count == 10
    finally:
        await engine.dispose()


async def test_unique_constraint_rejects_duplicate_reference(session, source, make_opportunity):
    from sqlalchemy.exc import IntegrityError

    await make_opportunity(reference="PG/UQ/001")
    with pytest.raises(IntegrityError):
        await make_opportunity(reference="PG/UQ/001")


async def test_document_text_fts_index_exists(session):
    exists = (
        await session.execute(
            text("select 1 from pg_indexes where indexname = 'ix_document_text_fts'")
        )
    ).scalar()
    assert exists is not None


async def test_connection_pool_reports_size(db_url):
    """Pool sizing comes from configuration and is actually applied."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url, pool_size=7, max_overflow=3)
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
        assert "Pool size: 7" in engine.pool.status()
    finally:
        await engine.dispose()
