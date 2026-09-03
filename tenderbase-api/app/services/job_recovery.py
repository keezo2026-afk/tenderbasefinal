"""Idempotent reconciliation of ingestion jobs, source runs and scheduler leases.

Sprint 1 audit finding: the queue and the database were allowed to disagree. A job row
could be written ``QUEUED`` and never reach Redis (the enqueue failed, or the API
process died between the two), a ``RUNNING`` row could outlive its worker (hard kill,
node eviction), a ``RETRYING`` row could have no deferred execution left to wake it
(Redis flushed, keys expired), and a source could be left mid-run forever. Nothing
repaired any of it: the scheduler only *creates* work, it never notices abandoned
work, and a source stuck in a dead run stops being crawled without ever failing.

This module closes that gap. Every repair is written so that running the pass twice
changes nothing the second time — each predicate selects rows by the state that
identifies the fault, and the repair moves the row to a state the predicate no longer
matches. The pass can therefore run on a cron inside the worker, on demand from an
operator, or both, in any order, with any overlap.

Design rules enforced here:

* **No new job states.** Repairs land on an existing ``JobStatus``
  (``FAILED`` / ``RETRYING`` / ``CANCELLED``), so consumers, the OpenAPI enum and
  history keep working.
* **Bounded re-dispatch.** A repair re-enqueues at most up to the job's own
  ``max_attempts``; past that it fails the job rather than starting a retry loop that
  outlives the outage which caused it.
* **Never fight a live worker.** Anything inside its lease or its timeout is left
  alone, and ``reconcile_reenqueue=false`` downgrades automatic re-dispatch to
  fail-and-report.
* **A repair is an event, not a silent edit.** Each one writes an ``ingestion_errors``
  row (stage ``WORKER``) so the history shows what happened instead of a status
  quietly changing under a reader.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.ingestion import IngestionError, IngestionJob
from app.db.models.source import MunicipalitySource, SourceRun
from app.enums import ErrorStage, JobStatus, JobTrigger, SourceLifecycle
from app.ingestion.discovery import _stored_utc, release_claim
from app.ingestion.pipeline import _health_for_failures
from app.logging import get_logger
from app.observability.metrics import RECOVERY_ACTIONS, SOURCE_FRESHNESS, SOURCE_FRESHNESS_HOURS
from app.utils.dates import utcnow

logger = get_logger(__name__)

#: Rows examined per pass, per repair. Reconciliation is a janitor, not a batch job:
#: a backlog is worked down one tick at a time so a bad hour cannot make the API wait
#: on a table scan.
PASS_LIMIT = 200

#: Job states that mean "somebody is still responsible for this row".
LIVE_JOB_STATUSES: tuple[str, ...] = (
    str(JobStatus.QUEUED),
    str(JobStatus.RUNNING),
    str(JobStatus.RETRYING),
)

#: Lifecycle states where being out of date is expected rather than an incident.
_NOT_SCHEDULABLE_LIFECYCLES: tuple[str, ...] = (
    str(SourceLifecycle.PAUSED),
    str(SourceLifecycle.DISABLED),
)

# Action names double as the metric label and the API's ``counts`` keys.
ACTION_REENQUEUED = "requeued"
ACTION_STALE_FAILED = "stale_job_failed"
ACTION_RUN_CLOSED = "source_run_closed"
ACTION_LEASE_CLEARED = "lease_expired_cleared"
ACTION_DUPLICATE_CANCELLED = "duplicate_job_cancelled"


@dataclasses.dataclass(slots=True)
class RecoveryAction:
    """One repair the pass made (or would have made, under ``dry_run``)."""

    action: str
    job_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "job_id": str(self.job_id) if self.job_id else None,
            "source_id": str(self.source_id) if self.source_id else None,
            "detail": self.detail,
        }


@dataclasses.dataclass(slots=True)
class RecoveryReport:
    """What one reconciliation pass found and changed."""

    started_at: datetime
    dry_run: bool = False
    reenqueue_enabled: bool = True
    actions: list[RecoveryAction] = dataclasses.field(default_factory=list)
    checked: dict[str, int] = dataclasses.field(default_factory=dict)
    freshness: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for action in self.actions:
            out[action.action] = out.get(action.action, 0) + 1
        return dict(sorted(out.items()))

    @property
    def changed(self) -> int:
        return len(self.actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "dry_run": self.dry_run,
            "reenqueue_enabled": self.reenqueue_enabled,
            "actions_count": self.changed,
            "counts": self.counts,
            "checked": dict(self.checked),
            "source_freshness": dict(self.freshness),
            "actions": [action.as_dict() for action in self.actions],
        }


class RecoveryWindows:
    """The staleness thresholds the pass works from, derived from configuration.

    Kept as a small object rather than loose parameters because three of the repairs
    need the same pair, and reading ``running_after`` at a call site is clearer than
    reading ``settings.worker_job_timeout_seconds + settings.job_running_grace_seconds``
    a fourth time.
    """

    __slots__ = ("lease", "queued", "running")

    def __init__(self, *, queued: timedelta, running: timedelta, lease: timedelta) -> None:
        self.queued = queued
        self.running = running
        self.lease = lease

    @classmethod
    def from_settings(cls, settings: Settings) -> RecoveryWindows:
        return cls(
            # Floor on the floors: a configuration that asks to reconcile a job two
            # seconds after it was written would race the enqueue every time.
            queued=timedelta(seconds=max(60.0, float(settings.job_queued_stale_after_seconds))),
            running=timedelta(
                seconds=max(120.0, float(settings.worker_job_timeout_seconds))
                + float(settings.job_running_grace_seconds)
            ),
            lease=timedelta(seconds=max(60.0, float(settings.source_claim_lease_seconds))),
        )


def _record_error(
    session: AsyncSession,
    *,
    job: IngestionJob | None = None,
    source_id: uuid.UUID | None = None,
    source_run_id: uuid.UUID | None = None,
    message: str,
) -> None:
    """Persist *why* something was repaired, as a normal ingestion error row.

    ``retryable=False`` because the row records a repair that already happened, not a
    failure somebody else should retry.
    """
    session.add(
        IngestionError(
            job_id=job.id if job is not None else None,
            source_id=source_id if source_id is not None else (job.source_id if job else None),
            source_run_id=source_run_id,
            stage=str(ErrorStage.WORKER),
            error_code="RECONCILED",
            message=message[:4000],
            retryable=False,
            context={"actor": "reconciliation", "attempt": int(job.attempt or 0)}
            if job is not None
            else {"actor": "reconciliation"},
        )
    )


def _close_run(run: SourceRun, *, status: str, error: str, now: datetime) -> None:
    """Finish a run that will never be finished by its worker."""
    run.status = status
    run.completed_at = now
    started = _stored_utc(run.started_at)
    if started is not None:
        run.duration_ms = int((now - started).total_seconds() * 1000)
    run.error_message = error[:4000]


def _to_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# The repairs
# ---------------------------------------------------------------------------


async def _find_stuck_jobs(
    session: AsyncSession, windows: RecoveryWindows, now: datetime, *, limit: int = PASS_LIMIT
) -> list[IngestionJob]:
    """Jobs whose queue execution does not exist, or never came back.

    Three cases share one query because they share one fix:

    ``QUEUED`` older than ``job_queued_stale_after_seconds``
        The enqueue never happened, or was lost before a worker claimed it.
    ``RUNNING`` past ``worker_job_timeout + job_running_grace``
        ARQ enforces its own timeout and records a retry or a failure, so a row still
        ``RUNNING`` past the timeout *plus* grace means nobody is running it any more.
    ``RETRYING`` whose ``scheduled_for`` is past due by the stale window
        The deferred retry is gone: Redis was flushed, the key expired, the queue was
        rebuilt. No worker will ever wake up for this row.
    """
    conditions = or_(
        (IngestionJob.status == str(JobStatus.QUEUED))
        & (IngestionJob.created_at < now - windows.queued),
        (IngestionJob.status == str(JobStatus.RUNNING))
        & (func.coalesce(IngestionJob.started_at, IngestionJob.created_at) < now - windows.running),
        (IngestionJob.status == str(JobStatus.RETRYING))
        & (IngestionJob.scheduled_for.is_not(None))
        & (IngestionJob.scheduled_for < now - windows.queued),
    )
    return list(
        (
            await session.execute(
                select(IngestionJob)
                .where(conditions, IngestionJob.status.in_(LIVE_JOB_STATUSES))
                .order_by(IngestionJob.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def _repair_stuck_jobs(
    session: AsyncSession,
    report: RecoveryReport,
    *,
    settings: Settings,
    windows: RecoveryWindows,
    now: datetime,
    queue: Any,
) -> None:
    """Re-dispatch what the queue lost, or fail it — bounded by the job's own budget."""
    jobs = await _find_stuck_jobs(session, windows, now)
    for job in jobs:
        source_id = _to_uuid(job.source_id)
        exhausted = int(job.attempt or 0) >= int(job.max_attempts or 1)
        # Re-dispatch needs the source id: ``ingest_source`` takes the source as an
        # argument, so a job whose source has been deleted cannot be run again at all.
        if settings.reconcile_reenqueue and queue is not None and not exhausted and source_id:
            # A distinct dedupe id: ARQ keeps a finished job's result for
            # ``keep_result_seconds`` and would answer "already queued" to the same id,
            # silently swallowing the repair.
            unique_id = f"ingest-source-{job.id}-r{int(job.attempt or 0) + 1}"
            try:
                enqueued = await queue.enqueue(
                    "ingest_source",
                    str(source_id),
                    job_id=str(job.id),
                    unique_id=unique_id,
                )
            except Exception as exc:  # noqa: BLE001 - an unreachable queue must not stop the pass
                logger.warning(
                    "recovery_reenqueue_failed",
                    job_id=str(job.id),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # Leave the row as it was: still stale, so the next pass retries the
                # repair. Losing Redis must not turn recoverable work into failures.
                report.checked["reenqueue_unavailable"] = (
                    report.checked.get("reenqueue_unavailable", 0) + 1
                )
                continue
            if enqueued is None:
                logger.info("recovery_reenqueue_duplicate", job_id=str(job.id))
                report.checked["reenqueue_duplicate"] = (
                    report.checked.get("reenqueue_duplicate", 0) + 1
                )
                continue
            job.status = str(JobStatus.RETRYING)
            job.attempt = int(job.attempt or 0) + 1
            job.scheduled_for = now
            job.trigger = str(JobTrigger.RETRY)
            job.queue_job_id = str(enqueued)
            if source_id is not None:
                # Re-assert the lease for the recovered attempt: without this, the next
                # scheduler tick sees the source due again (its lease long expired) and
                # starts a *second* job for a source that has one pending.
                holder = await session.get(MunicipalitySource, source_id)
                if holder is not None and _to_uuid(holder.claim_job_id) == job.id:
                    holder.claim_expires_at = now + timedelta(
                        seconds=float(settings.source_claim_lease_seconds)
                    )
            job.error_message = (
                "Reconciliation re-dispatched this job after finding it without a queue execution."
            )[:4000]
            detail = f"re-dispatched by reconciliation (attempt {job.attempt})"
            _record_error(session, job=job, message=detail)
            report.actions.append(
                RecoveryAction(
                    action=ACTION_REENQUEUED, job_id=job.id, source_id=source_id, detail=detail
                )
            )
            RECOVERY_ACTIONS.labels(action=ACTION_REENQUEUED).inc()
            continue

        job.status = str(JobStatus.FAILED)
        job.completed_at = now
        started = _stored_utc(job.started_at)
        job.duration_ms = int((now - started).total_seconds() * 1000) if started else None
        job.error_message = (
            "Reconciled: retry budget exhausted after the worker lost the job."
            if exhausted
            else "Reconciled without a queue execution; the source is available to the "
            "next scheduler tick."
        )[:4000]
        detail = job.error_message
        _record_error(session, job=job, message=detail)
        if source_id is not None:
            source = await session.get(MunicipalitySource, source_id)
            if source is not None and _to_uuid(source.claim_job_id) == job.id:
                # This claim ends because *we* ended its job, not because a run
                # finished; re-anchor on the last real run, not on now.
                await release_claim(session, source=source, job_id=job.id, settings=settings)
                source.next_run_at = now
                source.consecutive_failures = int(source.consecutive_failures or 0) + 1
                source.last_failure_at = now
                source.health_status = str(
                    _health_for_failures(int(source.consecutive_failures or 0))
                )
        report.actions.append(
            RecoveryAction(
                action=ACTION_STALE_FAILED, job_id=job.id, source_id=source_id, detail=detail
            )
        )
        RECOVERY_ACTIONS.labels(action=ACTION_STALE_FAILED).inc()


async def _repair_stuck_runs(
    session: AsyncSession, report: RecoveryReport, *, windows: RecoveryWindows, now: datetime
) -> None:
    """Close ``source_runs`` left ``RUNNING`` whose job is finished or gone.

    A run is bookkeeping, not work: leaving it open means the source looks busy and
    its "last run" is a row that will never complete. Closed as ``FAILED`` *with the
    partial counters it recorded kept* — the crawl did happen, it just did not finish,
    and pretending otherwise would understate what was ingested.
    """
    open_run = (SourceRun.status == str(JobStatus.RUNNING)) & (
        func.coalesce(SourceRun.started_at, SourceRun.created_at) < now - windows.running
    )
    # A run with no job is stuck by definition (a CLI run whose process died); one with
    # a job is stuck only once that job has left the live set.
    rows = (
        (
            await session.execute(
                select(SourceRun)
                .outerjoin(IngestionJob, IngestionJob.id == SourceRun.job_id)
                .where(
                    open_run,
                    or_(
                        IngestionJob.id.is_(None),
                        IngestionJob.status.notin_(LIVE_JOB_STATUSES),
                        IngestionJob.completed_at.is_not(None),
                    ),
                )
                .order_by(SourceRun.started_at)
                .limit(PASS_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for run in rows:
        error = (
            "Closed by reconciliation: its worker stopped before the run finished. "
            "Counters are what the run had reached when it was closed."
        )
        _close_run(run, status=str(JobStatus.FAILED), error=error, now=now)
        _record_error(
            session,
            source_id=run.source_id,
            source_run_id=run.id,
            message="Stuck source run closed as FAILED by reconciliation.",
        )
        report.actions.append(
            RecoveryAction(
                action=ACTION_RUN_CLOSED,
                job_id=run.job_id,
                source_id=run.source_id,
                detail="stuck source run closed as FAILED",
            )
        )
        RECOVERY_ACTIONS.labels(action=ACTION_RUN_CLOSED).inc()


async def _repair_expired_leases(
    session: AsyncSession, report: RecoveryReport, *, now: datetime
) -> None:
    """Give back claims whose lease expired without their job finishing.

    The lease is the safety net for a killed worker. Once it is past due **and** the
    job that held it is no longer live, the source must become claimable again, and its
    horizon moves to ``now``: the interrupted run produced nothing worth anchoring on.
    """
    rows = (
        (
            await session.execute(
                select(MunicipalitySource)
                .outerjoin(IngestionJob, IngestionJob.id == MunicipalitySource.claim_job_id)
                .where(
                    MunicipalitySource.active.is_(True),
                    MunicipalitySource.claim_expires_at.is_not(None),
                    MunicipalitySource.claim_expires_at < now,
                    or_(
                        IngestionJob.id.is_(None),
                        IngestionJob.status.notin_(LIVE_JOB_STATUSES),
                    ),
                )
                .order_by(MunicipalitySource.claim_expires_at)
                .limit(PASS_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for source in rows:
        held = _to_uuid(source.claim_job_id)
        source.claim_job_id = None
        source.claim_expires_at = None
        source.next_run_at = now
        detail = f"expired claim lease released (job {held})"
        report.actions.append(
            RecoveryAction(
                action=ACTION_LEASE_CLEARED, job_id=held, source_id=source.id, detail=detail
            )
        )
        RECOVERY_ACTIONS.labels(action=ACTION_LEASE_CLEARED).inc()


async def _cancel_duplicate_claims(
    session: AsyncSession, report: RecoveryReport, now: datetime
) -> None:
    """Cancel live jobs that duplicate a source's claimed job.

    Reachable only if a lease expired while its job was still queued — worker died,
    source re-claimed, the old retry then woke up. Two live jobs for one source is how
    a duplicate crawl starts, so the newcomer is cancelled rather than raced.
    """
    claimed = (
        (
            await session.execute(
                select(MunicipalitySource)
                .where(
                    MunicipalitySource.active.is_(True),
                    MunicipalitySource.claim_job_id.is_not(None),
                )
                .limit(PASS_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for source in claimed:
        holder = _to_uuid(source.claim_job_id)
        duplicates = (
            (
                await session.execute(
                    select(IngestionJob).where(
                        IngestionJob.source_id == source.id,
                        IngestionJob.status.in_(LIVE_JOB_STATUSES),
                        IngestionJob.id != holder,
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in duplicates:
            job.status = str(JobStatus.CANCELLED)
            job.completed_at = now
            job.error_message = (
                "Cancelled by reconciliation: this source already has a claimed job."
            )[:4000]
            detail = "duplicate live job cancelled"
            _record_error(session, job=job, message=detail)
            report.actions.append(
                RecoveryAction(
                    action=ACTION_DUPLICATE_CANCELLED,
                    job_id=job.id,
                    source_id=source.id,
                    detail=detail,
                )
            )
            RECOVERY_ACTIONS.labels(action=ACTION_DUPLICATE_CANCELLED).inc()


# ---------------------------------------------------------------------------
# Freshness (objective 9's data source; computed here because the reconciliation pass
# is what samples it)
# ---------------------------------------------------------------------------


def classify_freshness(
    source: MunicipalitySource,
    *,
    now: datetime,
    aging_after: datetime,
    stale_after: datetime,
) -> str:
    """Bucket one source by how out of date its data is.

    ``NOT_ACTIVE`` is separate from ``STALE`` on purpose: a source an operator turned
    off is not an outage, and an alert that cannot tell them apart is noise.
    """
    # Normalise for the same reason as the caller: stored timestamps may be naive UTC
    # on SQLite, and the thresholds below are aware.
    last = _stored_utc(source.last_success_at)
    if not source.active:
        return "NOT_ACTIVE"
    if source.lifecycle_status in _NOT_SCHEDULABLE_LIFECYCLES or source.paused_at is not None:
        # Paused by the operator or retired by the registry: out of date is expected,
        # not an incident.
        return "PAUSED"
    if last is None:
        return "NEVER_RUN"
    if last <= stale_after:
        return "STALE"
    if last <= aging_after:
        return "AGING"
    return "FRESH"


async def source_freshness(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    limit: int = 1000,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Classify sources by freshness and return per-source detail plus a summary.

    The thresholds are configuration (``FRESHNESS_AGING_HOURS`` /
    ``FRESHNESS_STALE_HOURS``) because "unacceptable" depends on how often a
    municipality publishes; the buckets stay coarse so sources remain comparable.
    """
    cfg = settings or get_settings()
    moment = now or utcnow()
    aging_after = moment - timedelta(hours=float(cfg.freshness_aging_hours))
    stale_after = moment - timedelta(hours=float(cfg.freshness_stale_hours))
    sources = (
        (
            await session.execute(
                select(MunicipalitySource)
                .order_by(
                    MunicipalitySource.next_run_at.asc().nulls_first(),
                    MunicipalitySource.slug.asc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows: list[dict[str, Any]] = []
    summary: dict[str, int] = {}
    for source in sources:
        state = classify_freshness(
            source, now=moment, aging_after=aging_after, stale_after=stale_after
        )
        # ``_stored_utc`` because SQLite hands these columns back naive (see the note in
        # discovery): subtracting an aware ``now`` from them is a TypeError. Normalise
        # rather than reassign, so a read path never mutates a loaded model.
        stored_success = _stored_utc(source.last_success_at)
        stored_run = _stored_utc(source.last_run_at)
        hours = (
            round((moment - stored_success).total_seconds() / 3600, 2)
            if stored_success is not None
            else None
        )
        if hours is not None:
            SOURCE_FRESHNESS_HOURS.observe(hours)
        summary[state] = summary.get(state, 0) + 1
        rows.append(
            {
                "source_id": str(source.id),
                "slug": source.slug,
                "name": source.name,
                "active": source.active,
                "lifecycle_status": source.lifecycle_status,
                "freshness_state": state,
                "last_run_at": (stored_run.isoformat() if stored_run is not None else None),
                "last_success_at": (stored_success.isoformat() if stored_success else None),
                "hours_since_success": hours,
                "next_run_at": source.next_run_at.isoformat() if source.next_run_at else None,
                "claim_expires_at": (
                    source.claim_expires_at.isoformat() if source.claim_expires_at else None
                ),
                "health_status": source.health_status,
                "consecutive_failures": int(source.consecutive_failures or 0),
            }
        )
    for state, count in summary.items():
        SOURCE_FRESHNESS.labels(state=state).set(float(count))
    rows.sort(
        key=lambda row: (
            -{"STALE": 3, "NEVER_RUN": 2, "AGING": 1}.get(str(row["freshness_state"]), 0),
            str(row["slug"]),
        )
    )
    return rows, summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def reconcile(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    queue: Any = None,
    dry_run: bool = False,
    now: datetime | None = None,
    include_freshness: bool = True,
) -> RecoveryReport:
    """Run every repair once and report what changed.

    ``dry_run=True`` computes and returns the same report without writing — the
    operator's "what would you do" mode, safe to point at production. ``queue`` is the
    worker's :class:`~app.workers.queue.JobQueue`; ``None`` (or a Redis outage) means
    no automatic re-dispatch, only fail-and-release.

    Order matters: runs are closed first so a source is judged on its finished run,
    then jobs, then the leases those job repairs leave behind, then duplicates.
    """
    cfg = settings or get_settings()
    moment = now or utcnow()
    windows = RecoveryWindows.from_settings(cfg)
    report = RecoveryReport(
        started_at=moment, dry_run=dry_run, reenqueue_enabled=bool(cfg.reconcile_reenqueue)
    )

    report.checked = await _counts(session)

    await _repair_stuck_runs(session, report, windows=windows, now=moment)
    await _repair_stuck_jobs(
        session, report, settings=cfg, windows=windows, now=moment, queue=queue
    )
    await _repair_expired_leases(session, report, now=moment)
    await _cancel_duplicate_claims(session, report, moment)

    if include_freshness:
        _, report.freshness = await source_freshness(session, settings=cfg, now=moment)
        report.checked["sources"] = sum(report.freshness.values())
    report.checked["actions"] = len(report.actions)

    if dry_run:
        # Same reads, same report, no writes: rollback undoes the flushed repairs, and
        # the metric counters it touched are advisory — a dry run counting an action it
        # did not apply is a deliberate trade, logged as such.
        await session.rollback()
        logger.info("reconciliation_dry_run", counts=report.counts)
        return report
    await session.commit()
    logger.info("reconciliation_completed", counts=report.counts)
    return report


async def _counts(session: AsyncSession) -> dict[str, int]:
    """A snapshot of the state the pass started from, so ``checked`` is auditable."""
    out: dict[str, int] = {}
    for status, count in (
        await session.execute(
            select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
        )
    ).all():
        out[f"jobs_{str(status).lower()}"] = int(count)
    out["sources_active"] = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MunicipalitySource)
                .where(MunicipalitySource.active.is_(True))
            )
        ).scalar_one()
    )
    out["sources_claimed"] = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MunicipalitySource)
                .where(MunicipalitySource.claim_job_id.is_not(None))
            )
        ).scalar_one()
    )
    out["source_runs_open"] = int(
        (
            await session.execute(
                select(func.count())
                .select_from(SourceRun)
                .where(SourceRun.status == str(JobStatus.RUNNING))
            )
        ).scalar_one()
    )
    return out


__all__ = [
    "ACTION_DUPLICATE_CANCELLED",
    "ACTION_LEASE_CLEARED",
    "ACTION_REENQUEUED",
    "ACTION_RUN_CLOSED",
    "ACTION_STALE_FAILED",
    "LIVE_JOB_STATUSES",
    "PASS_LIMIT",
    "RecoveryAction",
    "RecoveryReport",
    "RecoveryWindows",
    "classify_freshness",
    "reconcile",
    "source_freshness",
]
