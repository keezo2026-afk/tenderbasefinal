"""Distributed source claiming: leases, exclusivity, and what a claimer may touch.

Sprint 1.5 objective 1. The scheduler used to read "which sources are due" and enqueue
them; with two scheduler processes — a second replica, an overlapping cron tick, a
rolling restart — both read the same answer and created two jobs for the same source.
These tests pin the replacement: eligibility and ownership are decided in *one*
transaction over one row, so a second claimer either gets a different source or gets
nothing.

Two layers, deliberately:

* the logic (horizons, leases, lifecycle gates, backoff, holder-only release) runs on
  whatever backend the suite is using, so it is checked on every commit;
* the exclusivity itself (``FOR UPDATE SKIP LOCKED``) runs on PostgreSQL only, with a
  blocking claimer held open on purpose, because "the two claimers never agree on the
  same source" is a property of the database, not of the Python around it. On SQLite it
  skips with an explicit reason rather than pretending.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models.ingestion import IngestionJob
from app.db.models.source import MunicipalitySource
from app.enums import HealthStatus, JobStatus, JobTrigger, SourceLifecycle
from app.ingestion.discovery import (
    SCHEDULABLE_LIFECYCLES,
    claim_conditions,
    claim_due_sources,
    is_due,
    next_eligible_run,
    release_claim,
)
from app.utils.dates import utcnow

#: Same rule the suite's conftest uses; recomputed rather than imported so this file
#: does not depend on conftest internals for a boolean.
IS_POSTGRES = os.environ.get("TEST_DATABASE_URL", "").strip().startswith("postgresql")

pytestmark = pytest.mark.integration

NOW = utcnow()


def claim_settings(**overrides: Any) -> Settings:
    """Settings for the claim path, built per test instead of patched globally."""
    base: dict[str, Any] = {
        "app_env": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "source_claim_lease_seconds": 1800,
        "worker_job_timeout_seconds": 900,
    }
    base.update(overrides)
    return Settings(**base)


async def add_source(
    session: AsyncSession,
    slug: str,
    *,
    lifecycle: SourceLifecycle = SourceLifecycle.ACTIVE,
    crawl_frequency_minutes: int = 60,
    health: HealthStatus = HealthStatus.HEALTHY,
    active: bool = True,
    paused: bool = False,
    last_run_at: datetime | None = None,
    next_run_at: datetime | None = None,
    consecutive_failures: int = 0,
) -> MunicipalitySource:
    """A development fixture source, explicitly test data and never crawled."""
    source = MunicipalitySource(
        name=f"TEST FIXTURE source {slug}",
        slug=slug,
        organization="Test Fixture Municipality",
        base_url="https://example.org",
        municipality_id=None,
        connector_type="html.listing",
        connector_key="html.listing",
        lifecycle_status=str(lifecycle),
        verification_status="PASSED",
        verification_at=NOW - timedelta(days=1),
        crawl_frequency_minutes=crawl_frequency_minutes,
        health_status=str(health),
        consecutive_failures=consecutive_failures,
        active=active,
        paused_at=NOW if paused else None,
        paused_reason="test pause" if paused else None,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
    )
    session.add(source)
    await session.commit()
    return source


async def test_claim_records_job_horizon_and_lease(session: AsyncSession) -> None:
    """A claim is three writes and one job row, all from the same transaction."""
    await add_source(session, "claim-basic", crawl_frequency_minutes=60)
    settings = claim_settings()

    claims = await claim_due_sources(session, limit=5, settings=settings, now=NOW)
    assert len(claims) == 1
    claim = claims[0]
    source, job = claim.source, claim.job

    assert str(job.status) == str(JobStatus.QUEUED)
    assert str(job.trigger) == str(JobTrigger.SCHEDULER)
    assert job.source_id == source.id
    assert source.claim_job_id == job.id
    # The horizon is the crawl interval from *now*, and the lease is the configured
    # lease — both derived, neither hard-coded, so changing either setting moves them.
    assert source.next_run_at == NOW + timedelta(minutes=60)
    assert source.claim_expires_at == NOW + timedelta(seconds=1800)
    assert job.payload and "claimed_until" in job.payload

    await session.commit()


async def test_a_claimed_source_is_not_due_and_a_second_claimer_gets_nothing(
    session: AsyncSession,
) -> None:
    """Same source, two claimers: the second finds nothing while the lease is held."""
    await add_source(session, "claim-once")
    settings = claim_settings()

    first = await claim_due_sources(session, limit=10, settings=settings, now=NOW)
    await session.commit()
    assert [claim.source_id for claim in first] == [
        str((await source_by_slug(session, "claim-once")).id)
    ]

    second = await claim_due_sources(session, limit=10, settings=settings, now=NOW)
    assert second == []

    reloaded = await source_by_slug(session, "claim-once")
    assert is_due(reloaded, now=NOW) is False

    # Lease expiry and horizon are two different promises, and this is where they come
    # apart. At ``lease + 1s`` nobody owns the source any more — but the claim promised
    # the *next* crawl an interval from now, so the source is still not due. Being
    # unleased is what lets another worker take it; being due is what says it should.
    assert is_due(reloaded, now=NOW + timedelta(seconds=1801)) is False
    assert is_due(reloaded, now=NOW + timedelta(minutes=61)) is True


async def source_by_slug(session: AsyncSession, slug: str) -> MunicipalitySource:
    """Re-read a source from the database, not from this session's identity map.

    ``populate_existing`` is required, not a nicety: the fixtures hold their own session
    while the worker task commits through another one, and the session fixture uses
    ``expire_on_commit=False``. Without it, an assertion about what the task wrote would
    be comparing an object this test itself last modified.
    """
    stmt = (
        select(MunicipalitySource)
        .where(MunicipalitySource.slug == slug)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one()


async def test_concurrent_claimers_get_disjoint_sources(session: AsyncSession) -> None:
    """Different sources are still handed out while one source is claimed.

    Sequential here (SQLite has no row locks); the property that matters — that the
    second claimer skips a locked source rather than stopping for the whole batch — is
    asserted against real locks in the PostgreSQL test below.
    """
    slugs = [f"claim-many-{index}" for index in range(4)]
    for slug in slugs:
        await add_source(session, slug)
    settings = claim_settings()

    first = await claim_due_sources(session, limit=2, settings=settings, now=NOW)
    await session.commit()
    second = await claim_due_sources(session, limit=2, settings=settings, now=NOW)
    await session.commit()

    claimed = [claim.source_id for claim in first + second]
    assert len(claimed) == 4
    assert len(set(claimed)) == 4, "a source was handed out twice"


@pytest.mark.parametrize(
    ("lifecycle", "expected"),
    [
        (SourceLifecycle.ACTIVE, True),
        (SourceLifecycle.DEGRADED, True),
        (SourceLifecycle.VERIFIED, True),
        (SourceLifecycle.DISCOVERED, False),
        (SourceLifecycle.PENDING_VERIFICATION, False),
        (SourceLifecycle.PAUSED, False),
        (SourceLifecycle.DISABLED, False),
    ],
)
async def test_only_schedulable_lifecycle_states_are_claimed(
    session: AsyncSession, lifecycle: SourceLifecycle, expected: bool
) -> None:
    """Never-activated and retired sources are not queued, even with ``active`` true.

    The three schedulable states are derived from the enum, so this also fails if
    someone adds a lifecycle state without deciding whether it may be scheduled.
    """
    assert str(SourceLifecycle.VERIFIED) in SCHEDULABLE_LIFECYCLES
    await add_source(session, f"lifecycle-{lifecycle.value}", lifecycle=lifecycle)

    claims = await claim_due_sources(session, limit=5, settings=claim_settings(), now=NOW)
    assert bool(claims) is expected


async def test_paused_and_deactivated_sources_are_not_claimed(session: AsyncSession) -> None:
    await add_source(session, "paused", paused=True)
    await add_source(session, "inactive", active=False)

    assert await claim_due_sources(session, limit=5, settings=claim_settings(), now=NOW) == []
    # ``claim_conditions`` is the same predicate the read-only due list uses; a
    # disagreement there is a source that looks due forever or never.
    rows = (
        (await session.execute(select(MunicipalitySource).where(*claim_conditions(NOW))))
        .scalars()
        .all()
    )
    assert rows == []


async def test_failure_backoff_delays_the_next_claim(session: AsyncSession) -> None:
    """A FAILING source is claimed a quarter as often; an OFFLINE one, a twelfth."""
    now = utcnow()
    for health, multiplier in (
        (HealthStatus.HEALTHY, 1),
        (HealthStatus.DEGRADED, 2),
        (HealthStatus.FAILING, 4),
        (HealthStatus.OFFLINE, 12),
    ):
        source = await add_source(
            session,
            f"backoff-{health.value}",
            health=health,
            crawl_frequency_minutes=60,
            last_run_at=now,
        )
        horizon = next_eligible_run(source, now=now)
        assert horizon == now + timedelta(minutes=60 * multiplier)
        # Released after a run, the next claim must not be earlier than that horizon.
        assert is_due(source, now=now + timedelta(minutes=60 * multiplier - 1)) is False


async def test_next_eligible_run_can_anchor_on_the_finished_run(session: AsyncSession) -> None:
    """A 40-minute crawl on a 60-minute interval leaves 20 minutes, not a fresh hour."""
    now = utcnow()
    source = await add_source(session, "anchor", crawl_frequency_minutes=60, last_run_at=now)
    assert next_eligible_run(source, now=now + timedelta(minutes=40)) == now + timedelta(
        minutes=100
    )
    assert next_eligible_run(
        source, now=now + timedelta(minutes=40), from_last_run=True
    ) == now + timedelta(minutes=60)


async def test_only_the_holder_may_release_a_claim(session: AsyncSession) -> None:
    """A late job from an earlier claim must not strand the source's current owner."""
    source = await add_source(session, "holder")
    settings = claim_settings()
    claims = await claim_due_sources(session, limit=1, settings=settings, now=NOW)
    await session.commit()
    holder = claims[0].job.id

    # Somebody else's job id: refused, and the lease stays exactly where it was.
    assert await release_claim(session, source=source, job_id="0" * 32) is False
    assert source.claim_job_id == holder
    assert source.claim_expires_at is not None

    assert await release_claim(session, source=source, job_id=holder) is True
    assert source.claim_job_id is None
    assert source.claim_expires_at is None
    await session.commit()


async def test_release_with_reschedule_anchors_on_the_last_run(session: AsyncSession) -> None:
    now = utcnow()
    source = await add_source(session, "reschedule", crawl_frequency_minutes=60, last_run_at=now)
    claims = await claim_due_sources(session, limit=1, settings=claim_settings(), now=now)
    await session.commit()

    source.last_run_at = now + timedelta(minutes=40)
    released = await release_claim(session, source=source, job_id=claims[0].job.id, reschedule=True)
    assert released is True
    assert source.next_run_at == now + timedelta(minutes=100)


async def test_a_claim_without_a_lease_is_rejected_by_the_database(
    session: AsyncSession,
) -> None:
    """The CHECK that keeps ``claim_job_id`` and ``claim_expires_at`` together.

    Enforced in the schema rather than only in ``claim_due_sources`` because the failure
    mode of a future caller that forgets one of them is a source that never gets crawled
    again — silent, and diagnosed at 3 a.m.
    """
    source = await add_source(session, "check-lease")
    source.claim_job_id = source.id
    source.claim_expires_at = None
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_expired_lease_is_claimable_again(session: AsyncSession) -> None:
    """A lapsed lease with a passed horizon frees the source; a live lease never does."""
    now = utcnow()
    await add_source(session, "expired", crawl_frequency_minutes=600, last_run_at=now)
    claims = await claim_due_sources(session, limit=1, settings=claim_settings(), now=now)
    await session.commit()
    assert claims, "precondition: the source starts claimed"

    reloaded = await source_by_slug(session, "expired")
    assert is_due(reloaded, now=now + timedelta(seconds=30)) is False

    # A lease that has lapsed *and* a horizon that has passed makes the source
    # claimable again, which is the recovery path for a killed worker. The interval
    # throttle is anchored on ``last_run_at`` — the last run the pipeline actually
    # recorded — so a crashed crawl of a 10-hour source waits for its interval rather
    # than being re-queued immediately: the lease ends the ownership, not the schedule.
    later = now + timedelta(minutes=601)
    reloaded.next_run_at = later - timedelta(seconds=2)
    # Still inside the lease: the horizon has passed but somebody owns the source.
    reloaded.claim_expires_at = later + timedelta(seconds=10)
    assert is_due(reloaded, now=later - timedelta(seconds=1)) is False
    # Past the lease, the source is claimable again.
    reloaded.claim_expires_at = later - timedelta(seconds=2)
    assert is_due(reloaded, now=later) is True


# ---------------------------------------------------------------------------
# The worker task around the claims
# ---------------------------------------------------------------------------


class FakeQueue:
    """A queue that records what it was asked to run, and can fail on purpose."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail = fail

    async def enqueue(self, task: str, *args: Any, **kwargs: Any) -> str | None:
        if self.fail:
            raise RuntimeError("redis is down")
        self.calls.append((task, args, kwargs))
        return f"fake-{len(self.calls)}"


async def test_scheduler_task_releases_claims_when_enqueue_fails(
    session: AsyncSession, worker_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis outage must not cost a source its crawl slot.

    The claim is committed before anything touches the queue — that is what makes two
    schedulers safe — so the failure path has to give the claim back, or the source
    would sit unclaimable until the lease expires for no reason at all.
    """
    from app.workers import tasks as worker_tasks

    source = await add_source(session, "enqueue-fails")
    queue = FakeQueue(fail=True)
    monkeypatch.setattr("app.workers.queue.get_queue", lambda: queue)

    result = await worker_tasks.schedule_due_sources({}, limit=5)
    assert result["queued"] == 0
    assert result["released"] == 1

    reloaded = await source_by_slug(session, "enqueue-fails")
    assert reloaded.claim_job_id is None
    assert reloaded.claim_expires_at is None
    assert reloaded.next_run_at is not None  # left as claimed, then released

    job = (
        (await session.execute(select(IngestionJob).where(IngestionJob.source_id == source.id)))
        .scalars()
        .one()
    )
    assert str(job.status) == str(JobStatus.FAILED)
    assert "enqueue failed" in (job.error_message or "")


async def test_scheduler_task_queues_claims_it_took(
    session: AsyncSession, worker_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import tasks as worker_tasks

    await add_source(session, "enqueue-ok")
    queue = FakeQueue()
    monkeypatch.setattr("app.workers.queue.get_queue", lambda: queue)

    result = await worker_tasks.schedule_due_sources({}, limit=5)
    assert result["queued"] == 1
    assert len(queue.calls) == 1
    task, args, kwargs = queue.calls[0]
    assert task == "ingest_source"
    assert args == (result["source_ids"][0],)
    assert set(kwargs) == {"job_id"}

    source = await source_by_slug(session, "enqueue-ok")
    job = (
        (await session.execute(select(IngestionJob).where(IngestionJob.id == source.claim_job_id)))
        .scalars()
        .one()
    )
    assert job.queue_job_id == "fake-1"
    assert str(job.status) == str(JobStatus.QUEUED)


# ---------------------------------------------------------------------------
# Real exclusivity: PostgreSQL only
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.skipif(
    not IS_POSTGRES,
    reason=(
        "needs PostgreSQL: SKIP LOCKED and two overlapping transactions are the "
        "behaviour under test, and SQLite cannot run FOR UPDATE at all"
    ),
)
async def test_two_concurrent_claimers_never_share_a_source(db_url: str, engine: Any) -> None:
    """A claimer holding a source in an open transaction must not block the other.

    The first claimer claims one source and *does not commit*. The second must come back
    immediately with the remaining sources instead of waiting on the lock — that is the
    difference between ``FOR UPDATE SKIP LOCKED`` and a lock that turns the scheduler
    into a queue. The whole pass is wrapped in a timeout so a blocking implementation
    fails the test instead of hanging the suite.
    """
    engine = create_async_engine(db_url, pool_size=4, max_overflow=4)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    slugs = [f"pg-claim-{index}" for index in range(5)]
    async with factory() as setup:
        for slug in slugs:
            await add_source(setup, slug)

    held = asyncio.Event()
    release = asyncio.Event()
    settings = claim_settings()

    async def first_claimer() -> list[str]:
        async with factory() as session:
            claims = await claim_due_sources(session, limit=1, settings=settings, now=utcnow())
            held.set()
            await asyncio.wait_for(release.wait(), timeout=15)
            ids = [claim.source_id for claim in claims]
            await session.commit()
            return ids

    async def second_claimer() -> list[str]:
        await asyncio.wait_for(held.wait(), timeout=15)
        async with factory() as session:
            claims = await claim_due_sources(session, limit=25, settings=settings, now=utcnow())
            ids = [claim.source_id for claim in claims]
            await session.commit()
        # Hand the first claimer back its connection *here*: waiting for the gather to
        # finish would deadlock, because the first claimer is what the gather awaits.
        release.set()
        return ids

    try:
        first_ids, second_ids = await asyncio.wait_for(
            asyncio.gather(first_claimer(), second_claimer()), timeout=30
        )
    finally:
        await engine.dispose()

    assert len(first_ids) == 1
    assert len(second_ids) == 4, "SKIP LOCKED should hand out the unlocked rows"
    assert not set(first_ids) & set(second_ids), "a source was claimed by both workers"
    assert len(set(first_ids) | set(second_ids)) == 5


@pytest.mark.postgres
@pytest.mark.skipif(
    not IS_POSTGRES,
    reason="needs PostgreSQL: the check is that N claimers produce N disjoint sets",
)
async def test_many_concurrent_claimers_claim_each_source_once(db_url: str, engine: Any) -> None:
    """Eight claimers, four sources: four claims in total, no duplicates, no waiting.

    This is the shape of the real failure (two schedulers, one source, two crawls),
    played with enough concurrency that any window between reading and writing shows up.
    """
    engine = create_async_engine(db_url, pool_size=10, max_overflow=10)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as setup:
        for index in range(4):
            await add_source(setup, f"pg-race-{index}")
    settings = claim_settings()

    async def claimer() -> list[str]:
        async with factory() as session:
            claims = await claim_due_sources(session, limit=10, settings=settings, now=utcnow())
            await session.commit()
            return [claim.source_id for claim in claims]

    try:
        results = await asyncio.wait_for(asyncio.gather(*(claimer() for _ in range(8))), timeout=60)
    finally:
        await engine.dispose()

    claimed = [source_id for batch in results for source_id in batch]
    assert len(claimed) == 4, f"expected exactly four claims, got {len(claimed)}"
    assert len(set(claimed)) == 4


@pytest.mark.postgres
@pytest.mark.skipif(
    not IS_POSTGRES, reason="needs PostgreSQL to exercise the real migration and constraints"
)
async def test_claim_columns_exist_in_the_migrated_schema(db_url: str) -> None:
    """The claim columns and their index come from the migration, not from the models.

    ``Test`` asserts against the live PostgreSQL catalog: a model change without a
    revision would leave production unable to schedule anything at all, and this is the
    test that catches it (the SQLite path builds tables from the models and cannot).
    """
    from sqlalchemy import text

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as connection:
            columns = (
                (
                    await connection.execute(
                        text(
                            "select column_name from information_schema.columns "
                            "where table_name = 'municipality_sources' "
                            "and column_name in ('next_run_at', 'claim_expires_at', 'claim_job_id')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(columns) == ["claim_expires_at", "claim_job_id", "next_run_at"]
            index = (
                await connection.execute(
                    text(
                        "select indexname from pg_indexes where tablename = "
                        "'municipality_sources' and indexname = "
                        "'ix_municipality_sources_claim_due'"
                    )
                )
            ).scalar()
            assert index == "ix_municipality_sources_claim_due"
    finally:
        await engine.dispose()


def test_claim_condition_and_horizon_math_have_no_wall_clock() -> None:
    """``claim_conditions`` is a pure function of ``now``.

    A predicate that reads the wall clock internally cannot be tested for the boundary
    case that matters — the source that becomes due exactly at ``next_run_at``.
    """
    assert [str(clause) for clause in claim_conditions(datetime(2026, 1, 1, tzinfo=UTC))]
    source = MunicipalitySource(
        slug="pure",
        name="pure",
        organization="org",
        base_url="https://example.org",
        lifecycle_status=str(SourceLifecycle.ACTIVE),
        crawl_frequency_minutes=60,
        health_status=str(HealthStatus.HEALTHY),
        last_run_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        next_run_at=datetime(2026, 1, 1, 10, tzinfo=UTC),
        active=True,
    )
    assert is_due(source, now=datetime(2026, 1, 1, 9, 59, tzinfo=UTC)) is False
    assert is_due(source, now=datetime(2026, 1, 1, 10, 0, tzinfo=UTC)) is True
