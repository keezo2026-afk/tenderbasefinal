"""ARQ worker settings and cron schedule.

Run with::

    arq app.workers.scheduler.WorkerSettings
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


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info("worker.startup", environment=get_settings().app_env)


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.db.session import dispose_engine

    await dispose_engine()
    logger.info("worker.shutdown")


class WorkerSettings:
    """ARQ worker configuration."""

    functions = [ingest_source, schedule_due_sources, process_documents, monitor_source_health]
    cron_jobs = [
        # Queue due sources every 15 minutes; each source's own crawl_frequency
        # and health backoff decide whether it actually runs.
        cron(schedule_due_sources, minute={0, 15, 30, 45}, run_at_startup=False),
        # Drain the document download/extraction backlog every 5 minutes.
        cron(process_documents, minute=set(range(0, 60, 5)), run_at_startup=False),
        # Hourly source-health report.
        cron(monitor_source_health, minute={7}, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = QUEUE_NAME
    max_jobs = 5
    job_timeout = 900
    max_tries = 3
    keep_result = 3600
    health_check_interval = 60

    @property
    def redis_settings(self):  # noqa: ANN201 - ARQ reads this attribute
        return redis_settings()


# ARQ reads ``redis_settings`` from the class, not the instance.
WorkerSettings.redis_settings = redis_settings()  # type: ignore[assignment]
