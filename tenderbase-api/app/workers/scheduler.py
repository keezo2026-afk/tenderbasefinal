"""ARQ worker settings and cron schedule.

Run with::

    arq app.workers.scheduler.WorkerSettings

Every knob comes from application settings rather than a literal here, because a
deployment that raises ``WORKER_JOB_TIMEOUT_SECONDS`` and still gets killed after
15 minutes is worse than no setting at all. The cost of that choice is that ARQ
reads these attributes off the *class* at import time, so they are fixed per
worker process: restart the worker after changing them (documented in
``docs/DEPLOYMENT.md``).

The cron entries are the one place a *period* has to become a set of field values —
:func:`cron_ticks` does that translation for the configurable reconciliation interval and
reports when ARQ's cron cannot express what was asked.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from arq import cron

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.workers.queue import QUEUE_NAME, redis_settings
from app.workers.tasks import (
    ingest_source,
    monitor_source_health,
    process_documents,
    reconcile_jobs,
    schedule_due_sources,
)

logger = get_logger("tenderbase.workers")

_settings = get_settings()

#: Tick lengths ARQ's cron can express exactly. ``cron`` matches whole field values, so
#: a schedule only repeats cleanly when its period divides the field it lives in: every
#: 20 minutes does, every 45 minutes does not (00:00, 00:45, 01:30 — it drifts).
SECOND_TICKS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)
MINUTE_TICKS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)


@dataclasses.dataclass(frozen=True, slots=True)
class CronTick:
    """One ARQ cron entry: which field to fill, and with which values."""

    field: str
    values: frozenset[int]
    #: The period the entry actually runs at, in seconds.
    interval_seconds: int
    #: Whether that is the requested period, or the nearest longer one cron can express.
    exact: bool = True


def cron_ticks(interval_seconds: float) -> CronTick:
    """Translate a desired period into the ARQ cron expression that matches it.

    ARQ's cron takes sets of field values rather than an interval, so "every 5 minutes"
    has to become ``minute={0, 5, 10, ...}``. Sub-minute periods use the ``second``
    field, multi-hour ones the ``hour`` field. When the requested period cannot divide
    its field cleanly — 45 minutes does not divide an hour: 00:00, 00:45, 01:30 — the
    next longer one that does is used and ``exact`` is False. The caller logs that,
    because a silently-coarsened schedule is exactly the kind of configuration that
    looks applied and is not. The result is never *finer* than requested: recovery
    running late is a nuisance, recovery firing every second is an incident. The one
    exception is a period longer than a day, which cron cannot express at all — then it
    runs daily, still reported as inexact.
    """
    seconds = max(1.0, float(interval_seconds))
    if seconds < 60:
        chosen = next((tick for tick in SECOND_TICKS if tick >= seconds), None)
        if chosen is None:
            # Sub-minute but no divisor of a minute is close enough: "second 0" fires
            # once a minute, which is the finest promise cron can make here.
            return CronTick(
                field="second",
                values=frozenset({0}),
                interval_seconds=60,
                exact=False,
            )
        return CronTick(
            field="second",
            values=frozenset(range(0, 60, chosen)),
            interval_seconds=chosen,
            exact=abs(chosen - seconds) < 1e-9,
        )

    minutes = seconds / 60.0
    if minutes <= 60:
        chosen = next((tick for tick in MINUTE_TICKS if tick >= minutes), None)
        if chosen is None:
            return CronTick(
                field="minute",
                values=frozenset({0}),
                interval_seconds=3600,
                exact=abs(seconds - 3600) < 1e-9,
            )
        return CronTick(
            field="minute",
            values=frozenset(range(0, 60, chosen)),
            interval_seconds=chosen * 60,
            exact=abs(chosen - minutes) < 1e-9,
        )

    hours_requested = seconds / 3600.0
    # Only hour counts that tile a day can repeat cleanly; the first one at or above
    # what was asked keeps the promise of "never earlier".
    chosen_hours = next(
        (h for h in range(1, 25) if 24 % h == 0 and h >= hours_requested - 1e-9), None
    )
    if chosen_hours is None:
        # Past a day there is nothing coarser in a cron expression: run daily. Logged as
        # inexact, because it *is* earlier than asked and an operator must know.
        chosen_hours = 24
    return CronTick(
        field="hour",
        values=frozenset(range(0, 24, chosen_hours)),
        interval_seconds=chosen_hours * 3600,
        exact=abs(hours_requested - chosen_hours) < 1e-9,
    )


def cron_entry(func: Any, tick: CronTick, *, keep_result: int = 600) -> Any:
    """Build the ARQ cron object for a tick.

    Explicit branches instead of ``cron(func, **{field: values})``: ARQ's signature is
    typed field-by-field, and a star-unpacked dict of a union of those types is not
    something a type checker can accept — the spread would have to be silenced to keep
    the generality, which is worse than three lines.
    """
    values = set(tick.values)
    if tick.field == "second":
        return cron(func, second=values, keep_result=keep_result, run_at_startup=False)
    if tick.field == "minute":
        return cron(func, minute=values, keep_result=keep_result, run_at_startup=False)
    return cron(func, hour=values, keep_result=keep_result, run_at_startup=False)


_RECONCILE_TICK = cron_ticks(_settings.job_reconciliation_interval_seconds)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    if not _RECONCILE_TICK.exact:
        logger.warning(
            "worker.reconciliation_interval_coarsened",
            requested_seconds=_settings.job_reconciliation_interval_seconds,
            effective_seconds=_RECONCILE_TICK.interval_seconds,
            reason="ARQ cron cannot express that period exactly; the next longer "
            "cleanly-dividing tick is used instead, so recovery is slower than asked "
            "but never earlier.",
        )
    logger.info(
        "worker.startup",
        environment=_settings.app_env,
        queue=QUEUE_NAME,
        max_jobs=_settings.worker_max_jobs,
        job_timeout=_settings.worker_job_timeout_seconds,
        max_tries=_settings.worker_max_tries,
        reconcile_interval_seconds=_RECONCILE_TICK.interval_seconds,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.db.session import dispose_engine

    await dispose_engine()
    logger.info("worker.shutdown")


class WorkerSettings:
    """ARQ worker configuration.

    ``allow_abort_jobs`` is left off: a job killed between two statements of a
    commit is a worse outcome than the same job finishing and then stopping.
    """

    functions = [
        ingest_source,
        schedule_due_sources,
        process_documents,
        monitor_source_health,
        reconcile_jobs,
    ]
    cron_jobs = [
        # Queue due sources every 15 minutes; each source's own crawl interval and
        # failure backoff decide whether it actually runs, so this only *offers*
        # work — it does not hammer a source that ran a minute ago.
        cron(schedule_due_sources, minute={0, 15, 30, 45}, keep_result=600, run_at_startup=False),
        # Drain the document download/extraction backlog every 5 minutes.
        cron(process_documents, minute=set(range(0, 60, 5)), keep_result=600, run_at_startup=False),
        # Hourly source-health report (warnings only; it never mutates a source).
        cron(monitor_source_health, minute={7}, keep_result=600, run_at_startup=False),
        # Job/run/lease reconciliation on the configured interval (default 5 minutes).
        # Recovering lost work is a maintenance pass, so it runs on the worker rather
        # than the API: an API pod restart must not postpone it.
        cron_entry(reconcile_jobs, _RECONCILE_TICK),
    ]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = QUEUE_NAME
    max_jobs = _settings.worker_max_jobs
    job_timeout = _settings.worker_job_timeout_seconds
    max_tries = _settings.worker_max_tries
    keep_result = _settings.worker_keep_result_seconds
    health_check_interval = 60
    #: Redis connection parameters, resolved now: the ARQ CLI and
    #: ``create_worker`` both read this attribute off the class.
    redis_settings = redis_settings(_settings)
