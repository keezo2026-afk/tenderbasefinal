"""Periodic gauge snapshots.

Prometheus gauges that describe *stored state* (how many opportunities exist,
how deep the queue is, how many sources are unhealthy) cannot be incremented
from request paths without lying — a replica only sees its own traffic. They are
therefore recomputed on demand by the metrics scrape path and by the worker
scheduler, from the authoritative sources: PostgreSQL for data, Redis for the
queue.

Every operation here is optional and failure-isolated: a metrics scrape must
never 500 because a gauge could not be computed, and must never take a lock that
ingestion needs.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource
from app.logging import get_logger
from app.observability import metrics

logger = get_logger("tenderbase.observability.snapshot")


async def data_volume_gauges(session: AsyncSession) -> dict[str, object]:
    """Cheap aggregate counts used to refresh gauges."""
    values: dict[str, object] = {}
    try:
        values["tenders_total"] = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        select(ProcurementOpportunity.id)
                        .where(ProcurementOpportunity.is_test_fixture.is_(False))
                        .subquery()
                    )
                )
            ).scalar_one()
        )
    except Exception as exc:  # noqa: BLE001 - metrics must never break a request
        logger.warning("snapshot.tender_count_failed", error=str(exc))
    try:
        rows = (
            await session.execute(
                select(
                    MunicipalitySource.health_status,
                    func.max(MunicipalitySource.consecutive_failures),
                )
                .group_by(MunicipalitySource.health_status)
            )
        ).all()
        values["source_failures_by_health"] = {
            str(status): int(count or 0) for status, count in rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("snapshot.source_gauges_failed", error=str(exc))
    return values


async def queue_depth(settings: Settings | None = None) -> dict[str, int]:
    """Read ARQ queue length and running-job count straight from Redis.

    Returns zeros when Redis is unreachable: "queue depth unknown" is a metrics
    gap, not an outage, and the API must not look unhealthy because of it.
    """
    cfg = settings or get_settings()
    try:
        import redis.asyncio as aioredis

        from app.workers.queue import QUEUE_NAME

        client = aioredis.from_url(cfg.redis_url, decode_responses=True)
        try:
            # ARQ/redis-queue key layout: `<queue>` holds queued jobs,
            # `<queue>:in_process` a zset of executing jobs.
            depth = await client.llen(QUEUE_NAME)
            running = await client.zcard(f"{QUEUE_NAME}:in_process")
        finally:
            close = getattr(client, "aclose", None)
            await (close() if close else client.close())
        return {"queue_depth": int(depth or 0), "queue_running": int(running or 0)}
    except Exception as exc:  # noqa: BLE001
        logger.info("snapshot.queue_unavailable", error=str(exc))
        return {"queue_depth": 0, "queue_running": 0}


def database_pool_gauges(session: AsyncSession) -> dict[str, int]:
    """Sample the connection pool the request's engine is using.

    Reported rather than derived from latency: a saturated pool shows up as slow
    requests long before it shows up as an error, and "p99 latency" alone does not
    say whether the cause is PostgreSQL or the API waiting for a connection.

    ``overflow()`` is defined as ``checked_in + checked_out - size``, so it is
    negative until the pool is full — clamped here because a negative gauge makes
    a dashboard look broken. Engines without a sized pool (Static/Null, used by
    SQLite tests) implement none of it and are reported as "no data" instead of
    zeros that would look like an idle pool.
    """
    try:
        pool = session.sync_session.get_bind().pool
    except Exception:  # noqa: BLE001 - metrics must never break a scrape
        return {}
    gauges: dict[str, int] = {}
    for state, attribute in (
        ("size", "size"),
        ("checked_out", "checkedout"),
        ("checked_in", "checkedin"),
        ("overflow", "overflow"),
    ):
        method = getattr(pool, attribute, None)
        if method is None:
            continue
        try:
            value = int(method())
        except (NotImplementedError, TypeError, ValueError):  # pragma: no cover
            continue
        gauges[state] = max(value, 0) if state == "overflow" else value
    return gauges


async def refresh(
    settings: Settings | None = None, *, include_queue: bool = True
) -> dict[str, object]:
    """Recompute and publish all gauges. Returns the values for callers/tests."""
    from app.db.session import session_scope

    values: dict[str, object] = {}
    try:
        async with session_scope() as session:
            values.update(await data_volume_gauges(session))
            values["db_pool"] = database_pool_gauges(session)
    except Exception as exc:  # noqa: BLE001 - database down => skip gauge refresh
        logger.warning("snapshot.database_unavailable", error=str(exc))
    if include_queue:
        values.update(await queue_depth(settings))
    metrics.snapshot_gauges(values)
    return values
