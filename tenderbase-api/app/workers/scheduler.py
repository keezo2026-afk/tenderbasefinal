"""ARQ worker settings and cron schedule.

Run with::

    arq app.workers.scheduler.WorkerSettings

Every knob comes from application settings rather than a literal here, because a
deployment that raises ``WORKER_JOB_TIMEOUT_SECONDS`` and still gets killed after
15 minutes is worse than no setting at all. The cost of that choice is that ARQ
reads these attributes off the *class* at import time, so they are fixed per
worker process: restart the worker after changing them (documented in
``docs/DEPLOYMENT.md``).
"""

from __future__ import annotations

from typing import Any

from arq import cron

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.workers.queue import QUEUE_NAME, redis_settings
from app.workers.tasks import (
    ingest_source,
    monitor_source_health,
    process_documents,
    schedule_due_sources,
)

logger = get_logger("tenderbase.workers")

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info(
        "worker.startup",
        environment=_settings.app_env,
        queue=QUEUE_NAME,
        max_jobs=_settings.worker_max_jobs,
        job_timeout=_settings.worker_job_timeout_seconds,
        max_tries=_settings.worker_max_tries,
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

    functions = [ingest_source, schedule_due_sources, process_documents, monitor_source_health]
    cron_jobs = [
        # Queue due sources every 15 minutes; each source's own crawl interval and
        # failure backoff decide whether it actually runs, so this only *offers*
        # work — it does not hammer a source that ran a minute ago.
        cron(schedule_due_sources, minute={0, 15, 30, 45}, keep_result=600, run_at_startup=False),
        # Drain the document download/extraction backlog every 5 minutes.
        cron(process_documents, minute=set(range(0, 60, 5)), keep_result=600, run_at_startup=False),
        # Hourly source-health report (warnings only; it never mutates a source).
        cron(monitor_source_health, minute={7}, keep_result=600, run_at_startup=False),
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
