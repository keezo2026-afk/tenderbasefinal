"""Reconciliation: repairing jobs, runs and leases the queue and the database disagree about.

Sprint 1.5 objective 2. Every case here starts from a state a real deployment can reach —
a job row written and never dispatched, a worker killed mid-run, a Redis flush that
swallowed a deferred retry, a run whose worker never came back — and asserts three
things: the repair happens, it does **not** happen to a healthy row that merely looks
similar, and running the pass again finds nothing.

The third assertion is the one that matters operationally. A reconciliation pass is
meant to run forever, on a cron, alongside whatever else is happening; if a repair were
not idempotent it would re-fail, re-enqueue or re-cancel the same row every five minutes
and turn a recovered incident into a permanent one.

The API cases pin the contract of the operator endpoint: a mutating operations route
behind the ``admin`` scope, and ``dry_run`` meaning "tell me, change nothing".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models.ingestion import IngestionError, IngestionJob
from app.db.models.source import MunicipalitySource, SourceRun
from app.enums import ErrorStage, JobStatus, JobTrigger, SourceLifecycle
from app.services.job_recovery import (
    ACTION_DUPLICATE_CANCELLED,
    ACTION_LEASE_CLEARED,
    ACTION_REENQUEUED,
    ACTION_RUN_CLOSED,
    ACTION_STALE_FAILED,
    RecoveryWindows,
    classify_freshness,
    reconcile,
    source_freshness,
)
from app.utils.dates import utcnow
from app.workers import tasks as worker_tasks

pytestmark = pytest.mark.integration

#: Older than every staleness floor in :class:`RecoveryWindows` (60s queued, 120s
#: running), so a row back-dated by this is unambiguously stale.
LONG_AGO = timedelta(hours=6)


def recovery_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "source_claim_lease_seconds": 1800,
        "worker_job_timeout_seconds": 900,
        "job_queued_stale_after_seconds": 900,
        "job_running_grace_seconds": 300,
        "reconcile_reenqueue": True,
        "freshness_aging_hours": 36.0,
        "freshness_stale_hours": 96.0,
    }
    base.update(overrides)
    return Settings(**base)


class RecordingQueue:
    """Stands in for the worker's :class:`~app.workers.queue.JobQueue`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def enqueue(self, task: str, *args: Any, **kwargs: Any) -> str | None:
        if self.fail:
            raise ConnectionError("connection refused")
        self.calls.append({"task": task, "args": args, "kwargs": kwargs})
        return f"queued-{len(self.calls)}"


async def make_source(
    session: AsyncSession,
    slug: str,
    *,
    active: bool = True,
    lifecycle: SourceLifecycle = SourceLifecycle.ACTIVE,
    paused: bool = False,
    last_success: timedelta | None = None,
    claim: IngestionJob | None = None,
    lease: timedelta | None = None,
) -> MunicipalitySource:
    """A development-fixture source in the state the test needs."""
    now = utcnow()
    source = MunicipalitySource(
        name=f"TEST FIXTURE source {slug}",
        slug=slug,
        organization="Test Fixture Municipality",
        base_url="https://example.org",
        lifecycle_status=str(lifecycle),
        verification_status="PASSED",
        verification_at=now - LONG_AGO,
        crawl_frequency_minutes=60,
        health_status="HEALTHY",
        active=active,
        paused_at=now if paused else None,
        paused_reason="test pause" if paused else None,
        last_success_at=now - last_success if last_success is not None else None,
        claim_job_id=claim.id if claim is not None else None,
        claim_expires_at=now + lease if lease is not None else None,
    )
    session.add(source)
    await session.commit()
    return source


async def make_job(
    session: AsyncSession,
    source: MunicipalitySource,
    *,
    status: JobStatus,
    age: timedelta = LONG_AGO,
    attempt: int = 1,
    max_attempts: int = 3,
    started: timedelta | None = None,
    scheduled_for: timedelta | None = None,
    enqueue_id: str | None = None,
) -> IngestionJob:
    """A job row back-dated so its state is unambiguous.

    Timestamps are set explicitly rather than left to defaults: "stale" has to mean
    something precise for the thresholds under test to be tested.
    """
    now = utcnow()
    job = IngestionJob(
        source_id=source.id,
        job_type="SOURCE_INGEST",
        status=str(status),
        trigger=str(JobTrigger.SCHEDULER),
        attempt=attempt,
        max_attempts=max_attempts,
        scheduled_for=now + scheduled_for if scheduled_for else None,
        queue_job_id=enqueue_id,
        error_message=None,
    )
    session.add(job)
    await session.flush()
    job.created_at = now - age
    job.updated_at = now - age
    if started is not None:
        job.started_at = now - started
    await session.commit()
    return job


def _aware(value: Any) -> Any:
    """Treat a timestamp read back from SQLite as the UTC instant it represents.

    SQLite hands naive datetimes back, PostgreSQL aware ones; the assertions below mean
    the same thing on both because they compare through this.
    """
    from app.utils.dates import ensure_utc

    return ensure_utc(value, assume_timezone="UTC") if value is not None else None


async def reload_job(session: AsyncSession, job_id: UUID) -> IngestionJob:
    """Read the row back from the database, bypassing the identity map."""
    stmt = (
        select(IngestionJob)
        .where(IngestionJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one()


async def reload_source(session: AsyncSession, source_id: UUID) -> MunicipalitySource:
    stmt = (
        select(MunicipalitySource)
        .where(MunicipalitySource.id == source_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one()


# ---------------------------------------------------------------------------
# Queued but never dispatched
# ---------------------------------------------------------------------------


async def test_queued_job_that_was_never_enqueued_is_re_dispathed(session: AsyncSession) -> None:
    """The API wrote the row and died before Redis saw it."""
    source = await make_source(session, "lost-dispatch")
    job = await make_job(session, source, status=JobStatus.QUEUED, enqueue_id=None)
    queue = RecordingQueue()

    report = await reconcile(session, settings=recovery_settings(), queue=queue)

    assert report.counts == {ACTION_REENQUEUED: 1}
    assert queue.calls == [
        {
            "task": "ingest_source",
            "args": (str(source.id),),
            # The *job* id travels as the keyword, and the source id as the argument:
            # swapping them would run the wrong source with the wrong row.
            "kwargs": {"job_id": str(job.id), "unique_id": f"ingest-source-{job.id}-r2"},
        }
    ]
    fresh = await reload_job(session, job.id)
    assert str(fresh.status) == str(JobStatus.RETRYING)
    assert fresh.attempt == 2
    assert fresh.queue_job_id == "queued-1"
    assert fresh.trigger == str(JobTrigger.RETRY)

    # A repair that is not recorded is a repair nobody can explain afterwards.
    errors = (
        (await session.execute(select(IngestionError).where(IngestionError.job_id == job.id)))
        .scalars()
        .all()
    )
    assert [str(row.stage) for row in errors] == [str(ErrorStage.WORKER)]
    assert "reconciliation" in errors[0].context["actor"]


async def test_repeated_pass_after_a_repair_finds_nothing(session: AsyncSession) -> None:
    """Idempotency, asserted on the case most likely to be run twice by accident."""
    source = await make_source(session, "twice")
    await make_job(session, source, status=JobStatus.QUEUED)
    queue = RecordingQueue()
    settings = recovery_settings()

    first = await reconcile(session, settings=settings, queue=queue)
    assert first.changed == 1
    second = await reconcile(session, settings=settings, queue=queue)
    assert second.changed == 0
    assert second.actions == []
    assert len(queue.calls) == 1, "the second pass must not dispatch the job again"


async def test_recent_queued_job_is_left_alone(session: AsyncSession) -> None:
    """A job written seconds ago is in flight, not lost.

    The stale window exists precisely so reconciliation does not race the request that
    is still on its way to Redis; a pass that ignored it would double-dispatch every
    manual run under any latency at all.
    """
    source = await make_source(session, "just-queued")
    job = await make_job(session, source, status=JobStatus.QUEUED, age=timedelta(seconds=30))
    queue = RecordingQueue()

    report = await reconcile(session, settings=recovery_settings(), queue=queue)
    assert report.changed == 0
    assert queue.calls == []
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.QUEUED)


async def test_retry_budget_is_respected(session: AsyncSession) -> None:
    """The last attempt is failed, not requeued: reconciliation cannot outlive an outage."""
    source = await make_source(session, "exhausted")
    job = await make_job(session, source, status=JobStatus.QUEUED, attempt=3, max_attempts=3)
    queue = RecordingQueue()

    report = await reconcile(session, settings=recovery_settings(), queue=queue)
    assert report.counts == {ACTION_STALE_FAILED: 1}
    assert queue.calls == []
    fresh = await reload_job(session, job.id)
    assert str(fresh.status) == str(JobStatus.FAILED)
    assert "retry budget exhausted" in (fresh.error_message or "")


async def test_reenqueue_can_be_turned_off(session: AsyncSession) -> None:
    """``RECONCILE_REENQUEUE=false`` reports the fault instead of repairing it."""
    source = await make_source(session, "manual-recovery")
    job = await make_job(session, source, status=JobStatus.QUEUED)
    queue = RecordingQueue()

    report = await reconcile(
        session, settings=recovery_settings(reconcile_reenqueue=False), queue=queue
    )
    assert report.reenqueue_enabled is False
    assert report.counts == {ACTION_STALE_FAILED: 1}
    assert queue.calls == []
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.FAILED)


async def test_a_dead_queue_leaves_the_job_for_the_next_pass(session: AsyncSession) -> None:
    """Redis being down must not convert recoverable work into failures.

    The row stays stale and the pass reports it; the next tick repairs it when the queue
    comes back. Failing it instead would turn a 30-second Redis blip into lost crawls.
    """
    source = await make_source(session, "queue-down")
    job = await make_job(session, source, status=JobStatus.QUEUED)

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue(fail=True))
    assert report.changed == 0
    assert report.checked["reenqueue_unavailable"] == 1
    fresh = await reload_job(session, job.id)
    assert str(fresh.status) == str(JobStatus.QUEUED)

    # ...and it is still recoverable afterwards.
    recovered = RecordingQueue()
    second = await reconcile(session, settings=recovery_settings(), queue=recovered)
    assert second.counts == {ACTION_REENQUEUED: 1}


# ---------------------------------------------------------------------------
# Running, retrying, and the timeouts around them
# ---------------------------------------------------------------------------


async def test_running_job_past_its_timeout_is_re_dispatched_while_budget_remains(
    session: AsyncSession,
) -> None:
    """A worker killed mid-crawl, retries left: run it again.

    ``RUNNING`` past the timeout *plus* grace means ARQ's own watchdog never recorded a
    retry, so nobody is running this row any more. Recovery does not conclude the crawl
    was hopeless — it re-queues it under the job's own attempt budget.
    """
    source = await make_source(session, "dead-worker", lease=LONG_AGO)
    job = await make_job(
        session, source, status=JobStatus.RUNNING, started=timedelta(hours=2), attempt=1
    )
    held = await reload_source(session, source.id)
    held.claim_job_id = job.id
    held.claim_expires_at = utcnow() - timedelta(minutes=5)
    held.next_run_at = utcnow() + timedelta(minutes=30)
    await session.commit()
    queue = RecordingQueue()

    report = await reconcile(session, settings=recovery_settings(), queue=queue)
    assert report.counts == {ACTION_REENQUEUED: 1}
    assert queue.calls[0]["kwargs"]["job_id"] == str(job.id)

    fresh_job = await reload_job(session, job.id)
    assert str(fresh_job.status) == str(JobStatus.RETRYING)
    assert fresh_job.completed_at is None
    # The lease is re-asserted for the recovered attempt, so the next scheduler tick
    # cannot hand this source a *second* job while the recovered one is queued.
    after = await reload_source(session, source.id)
    assert _aware(after.claim_expires_at) > utcnow()
    assert after.claim_job_id == job.id


async def test_running_job_past_its_timeout_is_failed_when_budget_is_spent(
    session: AsyncSession,
) -> None:
    """Same fault, no retries left: fail the row and give the source back.

    Failing is where recovery stops. It is also the case that frees the claim — with no
    job left to run, holding a source hostage to a dead job's horizon helps nobody.
    """
    source = await make_source(session, "dead-worker-final", lease=LONG_AGO)
    job = await make_job(
        session,
        source,
        status=JobStatus.RUNNING,
        started=timedelta(hours=2),
        attempt=3,
        max_attempts=3,
    )
    held = await reload_source(session, source.id)
    held.claim_job_id = job.id
    held.claim_expires_at = utcnow() - timedelta(minutes=5)
    held.next_run_at = utcnow() + timedelta(minutes=30)
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue(fail=True))
    assert report.counts == {ACTION_STALE_FAILED: 1}

    fresh_job = await reload_job(session, job.id)
    assert str(fresh_job.status) == str(JobStatus.FAILED)
    assert fresh_job.completed_at is not None
    assert fresh_job.duration_ms is not None and fresh_job.duration_ms > 0

    after = await reload_source(session, source.id)
    assert after.claim_job_id is None
    assert after.claim_expires_at is None
    assert after.consecutive_failures == 1
    assert after.health_status == "DEGRADED"
    # Available again now, rather than at the horizon the never-finished run set.
    assert _aware(after.next_run_at) <= utcnow()


async def test_running_job_inside_its_timeout_is_not_touched(session: AsyncSession) -> None:
    """A crawl that is merely slow is still a crawl in progress."""
    source = await make_source(session, "slow-but-alive")
    job = await make_job(session, source, status=JobStatus.RUNNING, started=timedelta(minutes=10))

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert report.changed == 0
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.RUNNING)


async def test_retrying_job_with_no_future_execution_is_requeued(session: AsyncSession) -> None:
    """Redis was rebuilt: the deferred retry that was due at 03:00 is simply gone."""
    source = await make_source(session, "lost-retry")
    job = await make_job(
        session,
        source,
        status=JobStatus.RETRYING,
        scheduled_for=-timedelta(minutes=45),
        attempt=1,
    )
    queue = RecordingQueue()

    report = await reconcile(session, settings=recovery_settings(), queue=queue)
    assert report.counts == {ACTION_REENQUEUED: 1}
    assert job.id is not None and queue.calls[0]["kwargs"]["job_id"] == str(job.id)
    fresh = await reload_job(session, job.id)
    assert str(fresh.status) == str(JobStatus.RETRYING)
    assert fresh.attempt == 2


async def test_retrying_job_still_waiting_is_left_alone(session: AsyncSession) -> None:
    source = await make_source(session, "waiting-retry")
    job = await make_job(
        session,
        source,
        status=JobStatus.RETRYING,
        scheduled_for=timedelta(minutes=10),
    )

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert report.changed == 0
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.RETRYING)


async def test_failed_source_that_is_still_active_is_not_reenqueued(session: AsyncSession) -> None:
    """``FAILED`` is terminal: recovery does not resurrect failed jobs.

    A failed-but-active *source* is what the freshness report and the next scheduler
    tick are for. Re-queueing terminal jobs would retry a source that failed for a
    boring reason (404, moved URL) forever, and the "active" in the name is not a
    request to keep hammering it.
    """
    source = await make_source(session, "failed-but-active")
    job = await make_job(session, source, status=JobStatus.FAILED)

    queue = RecordingQueue()
    report = await reconcile(session, settings=recovery_settings(), queue=queue)
    assert report.changed == 0
    assert queue.calls == []
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.FAILED)


# ---------------------------------------------------------------------------
# Source runs and leases
# ---------------------------------------------------------------------------


async def test_stuck_source_run_is_closed_with_its_partial_counters(
    session: AsyncSession,
) -> None:
    """The bookkeeping row from a killed worker is finished, not deleted.

    Counters recorded before the crash stay: the crawl happened, it just did not
    complete. Zeroing them would understate what was ingested, and deleting the row
    would destroy the evidence that a run took place at all.
    """
    source = await make_source(session, "stuck-run")
    started = utcnow() - timedelta(hours=3)
    run = SourceRun(
        source_id=source.id,
        status=str(JobStatus.RUNNING),
        started_at=started,
        items_found=7,
        items_created=5,
        error_message=None,
    )
    session.add(run)
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=None)
    assert report.counts == {ACTION_RUN_CLOSED: 1}

    fresh = (
        (
            await session.execute(
                select(SourceRun)
                .where(SourceRun.id == run.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert str(fresh.status) == str(JobStatus.FAILED)
    assert fresh.completed_at is not None
    assert fresh.items_found == 7 and fresh.items_created == 5
    assert fresh.duration_ms and fresh.duration_ms > 6_000_000
    assert "reconciliation" in (fresh.error_message or "").lower()


async def test_open_run_whose_job_is_still_live_is_left_open(session: AsyncSession) -> None:
    source = await make_source(session, "run-in-progress")
    job = await make_job(session, source, status=JobStatus.RUNNING, started=timedelta(minutes=5))
    run = SourceRun(
        source_id=source.id,
        job_id=job.id,
        status=str(JobStatus.RUNNING),
        started_at=utcnow() - timedelta(hours=3),
    )
    session.add(run)
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert report.counts.get(ACTION_RUN_CLOSED, 0) == 0
    open_runs = (
        await session.execute(
            select(func.count()).select_from(SourceRun).where(SourceRun.id == run.id)
        )
    ).scalar_one()
    assert open_runs == 1


async def test_expired_lease_without_a_live_job_is_cleared(session: AsyncSession) -> None:
    """The stuck state a killed worker leaves behind, repaired without a job at all."""
    source = await make_source(session, "orphan-lease")
    # A claim whose job row was deleted (or never created) and whose lease has lapsed.
    source.claim_job_id = uuid4()
    source.claim_expires_at = utcnow() - timedelta(minutes=1)
    source.next_run_at = utcnow() + timedelta(days=1)
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=None)
    assert report.counts == {ACTION_LEASE_CLEARED: 1}
    after = await reload_source(session, source.id)
    assert after.claim_job_id is None
    assert after.claim_expires_at is None
    assert _aware(after.next_run_at) <= utcnow()


async def test_live_lease_is_never_broken(session: AsyncSession) -> None:
    source = await make_source(session, "healthy-lease", lease=timedelta(minutes=20))
    job = await make_job(session, source, status=JobStatus.RUNNING, started=timedelta(minutes=2))
    fresh_source = await reload_source(session, source.id)
    fresh_source.claim_job_id = job.id
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert report.changed == 0
    after = await reload_source(session, source.id)
    assert after.claim_job_id == job.id


async def test_duplicate_live_job_for_a_claimed_source_is_cancelled(session: AsyncSession) -> None:
    """The leftover of a reclaimed source: the claim holder wins, the newcomer is cancelled.

    This is the state in which two crawls of the same source would run at once — the
    exact failure objective 1 exists to prevent. If one ever appears (a lease that
    lapsed mid-flight, then a re-claim before the old retry woke up) it is closed rather
    than raced.
    """
    source = await make_source(session, "duplicate", lease=timedelta(minutes=20))
    holder = await make_job(session, source, status=JobStatus.RUNNING, started=timedelta(minutes=1))
    stale_source = await reload_source(session, source.id)
    stale_source.claim_job_id = holder.id
    await session.commit()
    duplicate = await make_job(session, source, status=JobStatus.QUEUED, age=timedelta(minutes=2))

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert report.counts.get(ACTION_DUPLICATE_CANCELLED) == 1
    cancelled = await reload_job(session, duplicate.id)
    assert str(cancelled.status) == str(JobStatus.CANCELLED)
    assert "already has a claimed job" in (cancelled.error_message or "")
    # The holder keeps running.
    assert str((await reload_job(session, holder.id)).status) == str(JobStatus.RUNNING)


# ---------------------------------------------------------------------------
# Dry run and reporting
# ---------------------------------------------------------------------------


async def test_dry_run_reports_without_writing_anything(session: AsyncSession) -> None:
    """``dry_run`` is the mode an operator points at production first.

    Asserted on every kind of pending repair at once, because the rollback has to undo
    all of them: a dry run that closed a run or cleared a lease while claiming not to
    would be worse than no dry run at all.
    """
    source = await make_source(session, "dry-run", lease=timedelta(seconds=-1))
    queued = await make_job(session, source, status=JobStatus.QUEUED)
    run = SourceRun(
        source_id=source.id,
        status=str(JobStatus.RUNNING),
        started_at=utcnow() - timedelta(hours=4),
    )
    session.add(run)
    await session.commit()
    # Ids, not rows: a dry run rolls the session back, which expires every object this
    # test holds, and touching one afterwards is an unawaited load.
    source_id, job_id, run_id = source.id, queued.id, run.id

    report = await reconcile(
        session, settings=recovery_settings(), queue=RecordingQueue(), dry_run=True
    )
    assert report.dry_run is True
    assert report.changed >= 1

    assert str((await reload_job(session, job_id)).status) == str(JobStatus.QUEUED)
    assert (await reload_source(session, source_id)).claim_expires_at is not None
    reloaded_run = (
        (
            await session.execute(
                select(SourceRun)
                .where(SourceRun.id == run_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert str(reloaded_run.status) == str(JobStatus.RUNNING)

    # And the same pass, for real, now does change them.
    applied = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    assert applied.changed == report.changed
    assert str((await reload_job(session, job_id)).status) == str(JobStatus.RETRYING)


async def test_windows_are_floored_and_derived_from_settings() -> None:
    """The thresholds are configurable, and cannot be configured into a race.

    A 1-second stale window would have reconciliation racing the request that just
    created the row, so the windows have floors; the floors are asserted rather than
    trusted, because the settings themselves allow smaller values.
    """
    tiny = RecoveryWindows.from_settings(
        recovery_settings(
            job_queued_stale_after_seconds=30,
            job_running_grace_seconds=0,
            worker_job_timeout_seconds=10,
            source_claim_lease_seconds=30,
        )
    )
    assert tiny.queued >= timedelta(seconds=60)
    assert tiny.running >= timedelta(seconds=120)
    assert tiny.lease >= timedelta(seconds=60)

    real = RecoveryWindows.from_settings(
        recovery_settings(
            job_queued_stale_after_seconds=1200,
            worker_job_timeout_seconds=600,
            job_running_grace_seconds=60,
            source_claim_lease_seconds=3600,
        )
    )
    assert real.queued == timedelta(seconds=1200)
    assert real.running == timedelta(seconds=660)
    assert real.lease == timedelta(seconds=3600)


async def test_report_shape_is_machine_readable(session: AsyncSession) -> None:
    """The counts are what an alert reads; ``checked`` is what makes 0 trustworthy."""
    source = await make_source(session, "shape")
    await make_job(session, source, status=JobStatus.QUEUED)

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    payload = report.as_dict()
    assert set(payload) == {
        "started_at",
        "dry_run",
        "reenqueue_enabled",
        "actions_count",
        "counts",
        "checked",
        "source_freshness",
        "actions",
    }
    assert payload["actions_count"] == 1
    assert payload["checked"]["sources_active"] == 1
    assert payload["source_freshness"]  # sampled in the same pass
    assert payload["actions"][0]["job_id"]


# ---------------------------------------------------------------------------
# Freshness (objective 9)
# ---------------------------------------------------------------------------


async def test_freshness_classification_and_thresholds(session: AsyncSession) -> None:
    """One source per bucket, thresholds read from settings, worst first.

    The exact boundary is asserted for one case: a source is ``AGING`` the moment its
    last success is older than the threshold, which is what makes "did the schedule
    slip or did the source break" answerable from the API.
    """
    settings = recovery_settings(freshness_aging_hours=12.0, freshness_stale_hours=48.0)
    await make_source(session, "fresh", last_success=timedelta(hours=1))
    await make_source(session, "aging", last_success=timedelta(hours=20))
    await make_source(session, "stale", last_success=timedelta(hours=72))
    await make_source(session, "never-run")
    await make_source(session, "paused", last_success=timedelta(hours=1), paused=True)
    await make_source(session, "inactive", last_success=timedelta(hours=1), active=False)
    await make_source(
        session, "disabled", last_success=timedelta(hours=1), lifecycle=SourceLifecycle.DISABLED
    )

    rows, summary = await source_freshness(session, settings=settings)
    by_slug = {row["slug"]: row for row in rows}
    assert {slug: row["freshness_state"] for slug, row in by_slug.items()} == {
        "fresh": "FRESH",
        "aging": "AGING",
        "stale": "STALE",
        "never-run": "NEVER_RUN",
        "paused": "PAUSED",
        "inactive": "NOT_ACTIVE",
        "disabled": "PAUSED",
    }
    assert summary == {
        "FRESH": 1,
        "AGING": 1,
        "STALE": 1,
        "NEVER_RUN": 1,
        "PAUSED": 2,
        "NOT_ACTIVE": 1,
    }
    # Worst first, so a dashboard's first row is the row that needs reading.
    assert rows[0]["slug"] == "stale"
    assert by_slug["stale"]["hours_since_success"] == pytest.approx(72.0, abs=0.1)
    assert by_slug["never-run"]["hours_since_success"] is None
    assert by_slug["fresh"]["last_success_at"].endswith("+00:00")


async def test_classify_freshness_boundary_is_inclusive(session: AsyncSession) -> None:
    """The aging threshold is inclusive, and one second earlier is not.

    Thresholds are derived from the row's own stored timestamp rather than from a clock
    reading in the test: ``make_source`` stamps ``utcnow()`` itself, and comparing a
    stored instant against a *second* clock reading makes the boundary off by the
    microseconds between them — a test that fails for reasons of scheduling, not logic.
    """
    source = await make_source(session, "boundary", last_success=timedelta(hours=12))
    last = _aware(source.last_success_at)
    assert last is not None
    observed_at = last + timedelta(hours=12)

    at_threshold = classify_freshness(
        source, now=observed_at, aging_after=last, stale_after=last - timedelta(hours=1)
    )
    assert at_threshold == "AGING"

    # One second of slack on the cutoff and the same run is fresh: the comparison is
    # "last success at or before the cutoff", not "within a window of it".
    inside_by_one_second = classify_freshness(
        source,
        now=observed_at,
        aging_after=last - timedelta(seconds=1),
        stale_after=last - timedelta(hours=1),
    )
    assert inside_by_one_second == "FRESH"

    past_the_stale_threshold = classify_freshness(
        source,
        now=observed_at,
        aging_after=last - timedelta(hours=1),
        stale_after=last + timedelta(seconds=1),
    )
    assert past_the_stale_threshold == "STALE"


# ---------------------------------------------------------------------------
# The worker cron task
# ---------------------------------------------------------------------------


async def test_reconcile_task_reports_and_caps_the_sample(
    session: AsyncSession, worker_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cron task's return value is the same report the endpoint returns."""
    source = await make_source(session, "cron-task")
    for index in range(4):
        await make_job(
            session, source, status=JobStatus.QUEUED, age=LONG_AGO + timedelta(minutes=index)
        )
    monkeypatch.setattr("app.workers.queue.get_queue", lambda: RecordingQueue())

    result = await worker_tasks.reconcile_jobs({})
    assert result["dry_run"] is False
    assert result["actions_count"] >= 1
    assert len(result["actions"]) <= worker_tasks.RECOVERY_ACTION_SAMPLE
    assert ACTION_REENQUEUED in result["counts"] or ACTION_STALE_FAILED in result["counts"]


async def test_reconcile_task_survives_a_missing_queue(
    session: AsyncSession, worker_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Any:
        raise RuntimeError("redis configuration is broken")

    monkeypatch.setattr("app.workers.queue.get_queue", boom)
    result = await worker_tasks.reconcile_jobs({})
    assert result["actions_count"] == 0


# ---------------------------------------------------------------------------
# The operator endpoint
# ---------------------------------------------------------------------------


async def test_freshness_endpoint_reports_and_filters(client: Any) -> None:
    """The list is a plain JSON array in the standard envelope; the filter is validated.

    A bad ``state`` is a 422 naming the query field rather than an empty list: an
    operator who typoed a bucket should find out immediately.
    """
    response = await client.get("/api/v1/operations/sources/freshness")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"data", "meta"}
    assert isinstance(body["data"], list)

    bad = await client.get("/api/v1/operations/sources/freshness", params={"state": "BOGUS"})
    assert bad.status_code == 422
    error = bad.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["errors"][0]["field"] == "query.state"


async def test_reconcile_endpoint_dry_run_changes_nothing(
    make_client: Any, session: AsyncSession
) -> None:
    source = await make_source(session, "api-dry-run")
    job = await make_job(session, source, status=JobStatus.QUEUED)

    client = await make_client()
    response = await client.post("/api/v1/operations/reconcile", params={"dry_run": "true"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["actions_count"] >= 0
    assert set(data) >= {"counts", "checked", "source_freshness", "actions"}
    assert str((await reload_job(session, job.id)).status) == str(JobStatus.QUEUED)


async def test_reconcile_endpoint_requires_admin_scope(
    make_client: Any, session: AsyncSession
) -> None:
    """A mutating operations route is not available to every read-only key.

    ``/operations`` reads are ``read:sources``; this one re-dispatches queued work, so
    it asks for ``admin`` and the refusal names the scope that would be needed.
    """
    from tests.integration.test_api_security import issue_key

    client = await make_client(api_key_enforcement_enabled=True)

    anonymous = await client.post("/api/v1/operations/reconcile")
    assert anonymous.status_code == 401

    reader = await issue_key(client, session, scopes=["read:sources"])
    refused = await client.post("/api/v1/operations/reconcile", headers={"X-API-Key": reader})
    assert refused.status_code == 403
    assert "admin" in refused.text

    admin = await issue_key(client, session, scopes=["admin"])
    allowed = await client.post(
        "/api/v1/operations/reconcile",
        params={"dry_run": "true"},
        headers={"X-API-Key": admin},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.headers["content-type"].startswith("application/json")


async def test_no_new_job_states_are_introduced(session: AsyncSession) -> None:
    """Recovery reuses the vocabulary; ``JobStatus`` is not extended for it.

    Every action in the report must land on a status that already existed, or every
    consumer of ``ingestion_jobs.status`` — the operations endpoints, the OpenAPI enum,
    anything built on it — silently gains a case it does not handle.
    """
    known = {str(member) for member in JobStatus}
    source = await make_source(session, "vocabulary")
    for status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING):
        await make_job(session, source, status=status, started=timedelta(hours=3))
    run = SourceRun(
        source_id=source.id,
        status=str(JobStatus.RUNNING),
        started_at=utcnow() - timedelta(hours=5),
    )
    session.add(run)
    await session.commit()

    report = await reconcile(session, settings=recovery_settings(), queue=RecordingQueue())
    statuses = {
        str(row.status)
        for row in (
            await session.execute(select(IngestionJob).where(IngestionJob.source_id == source.id))
        )
        .scalars()
        .all()
    }
    assert statuses <= known, statuses - known
    assert report.actions, "the pass should have repaired something here"
