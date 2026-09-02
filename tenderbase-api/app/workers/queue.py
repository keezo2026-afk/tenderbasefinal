"""Job queue abstraction (ARQ / Redis).

Redis is required for background ingestion, **not** for serving the read API —
the API never touches the queue on a request path. When Redis is unavailable
the queue degrades to an explicit error rather than silently dropping work.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.errors import ServiceUnavailableError
from app.logging import get_logger

logger = get_logger("tenderbase.workers.queue")

QUEUE_NAME = "tenderbase:ingestion"


def redis_settings(settings: Settings | None = None):  # noqa: ANN201
    """Build ARQ Redis settings from ``REDIS_URL``."""
    from arq.connections import RedisSettings

    cfg = settings or get_settings()
    return RedisSettings.from_dsn(cfg.redis_url)


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

    async def enqueue(self, task: str, *args: Any, **kwargs: Any) -> str | None:
        """Enqueue a task, returning the queue job id."""
        pool = await self.connect()
        job = await pool.enqueue_job(task, *args, _queue_name=QUEUE_NAME, **kwargs)
        job_id = getattr(job, "job_id", None)
        logger.info("queue.enqueued", task=task, queue_job_id=job_id)
        return job_id

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
