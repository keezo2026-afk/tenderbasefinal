"""Job queue abstraction (ARQ / Redis).

Redis is required for background ingestion, **not** for serving the read API —
the API never touches the queue on a request path. When Redis is unavailable
the queue degrades to an explicit error rather than silently dropping work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.errors import ServiceUnavailableError
from app.logging import get_logger

logger = get_logger("tenderbase.workers.queue")

QUEUE_NAME = "tenderbase:ingestion"


def redis_settings(settings: Settings | None = None):  # noqa: ANN201 - ARQ type is optional
    """Build ARQ Redis settings from ``REDIS_URL``.

    Validated here, with a message that names the variable, because this runs at
    worker import time: the raw ``ValueError`` from ``from_dsn`` points at a URL
    the operator cannot see and does not mention ``REDIS_URL``.
    """
    from arq.connections import RedisSettings

    cfg = settings or get_settings()
    url = (cfg.redis_url or "").strip()
    if not url:
        raise ValueError("REDIS_URL is empty; the ingestion worker needs a Redis URL")
    if "://" not in url:
        raise ValueError(f"REDIS_URL must include a scheme (redis:// or rediss://); got {url!r}")
    try:
        return RedisSettings.from_dsn(url)
    except Exception as exc:  # noqa: BLE001 - re-raised with the config name attached
        raise ValueError(f"REDIS_URL is not a valid Redis DSN: {url!r} ({exc})") from exc


class JobQueue:
    """Thin wrapper over an ARQ Redis pool."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool: Any | None = None

    async def connect(self) -> Any:
        if self._pool is None:
            from arq import create_pool

            try:
                self._pool = await create_pool(redis_settings(self.settings))
            except Exception as exc:  # noqa: BLE001 - surfaced as a clear error
                raise ServiceUnavailableError(
                    f"Could not connect to Redis at {self.settings.redis_url}",
                    code="QUEUE_UNAVAILABLE",
                ) from exc
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def enqueue(
        self,
        task: str,
        *args: Any,
        defer_seconds: float | None = None,
        defer_until: datetime | None = None,
        unique_id: str | None = None,
        expires: float | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Enqueue a task and return the queue job id, or ``None`` if superseded.

        The underscore-free names here are deliberate: anything in ``kwargs`` is
        passed to the task itself, so a parameter called ``job_id`` would silently
        be stolen from a task that wants its own ``job_id`` (ours does — the
        ``ingestion_jobs`` row). Queue options therefore have their own names, and
        ``unique_id`` maps to ARQ's ``_job_id`` dedupe key.

        Deferring is how the retry policy backs off without blocking a worker
        slot: the job sits in the queue with a future score and is invisible to
        ``zcard``-style depth until it is due.
        """
        if defer_seconds and defer_until:
            raise ValueError("use either defer_seconds or defer_until, not both")
        pool = await self.connect()
        options: dict[str, Any] = {"_queue_name": QUEUE_NAME}
        if defer_seconds:
            options["_defer_by"] = timedelta(seconds=defer_seconds)
        if defer_until is not None:
            options["_defer_until"] = defer_until
        if expires is not None:
            options["_expires"] = timedelta(seconds=expires)
        if unique_id:
            options["_job_id"] = unique_id
        job = await pool.enqueue_job(task, *args, **options, **kwargs)
        if job is None:
            # ARQ returns None when that id is already queued or its result is
            # kept: a duplicate enqueue is a no-op, which is what makes the
            # scheduler safe to run twice in a row.
            logger.info("queue.duplicate_ignored", task=task, unique_id=unique_id)
            return None
        job_id = getattr(job, "job_id", None)
        logger.info(
            "queue.enqueued",
            task=task,
            queue_job_id=job_id,
            deferred_seconds=defer_seconds,
            deferred_until=defer_until,
        )
        return job_id

    async def job_result(self, job_id: str) -> Any:
        """Result of a finished job, or ``None`` while it is still queued."""
        from arq.jobs import Job

        pool = await self.connect()
        job = Job(job_id, pool)
        return await job.result(timeout=5)

    async def health(self) -> bool:
        """True when Redis answers a ping."""
        try:
            pool = await self.connect()
            await pool.ping()
        except Exception:  # noqa: BLE001
            return False
        return True


_queue: JobQueue | None = None


def get_queue(settings: Settings | None = None) -> JobQueue:
    """Process-wide queue instance."""
    global _queue
    if _queue is None:
        _queue = JobQueue(settings)
    return _queue
