"""Discovery engine.

Two kinds of discovery:

1. **Source discovery** — which configured sources are *due* to run, based on
   their lifecycle, pause flag, activation state, crawl frequency and health
   backoff.
2. **Target discovery** — delegating to a connector to enumerate the URLs it
   intends to fetch for a source.

Discovery never invents sources. It only schedules what operators registered and
then *activated*: a source that nobody has verified, or that somebody paused, is
skipped even though ``active`` is still true. The skip is counted and logged, so
"why is this source not crawling" is answerable from the worker log rather than
being a mystery.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import DiscoveryTarget, ProcurementConnector, SourceContext
from app.db.models.source import MunicipalitySource
from app.enums import HealthStatus, SourceLifecycle
from app.logging import get_logger
from app.utils.dates import utcnow

logger = get_logger("tenderbase.discovery")

#: Failing sources are backed off rather than hammered.
BACKOFF_MULTIPLIER: dict[HealthStatus, float] = {
    HealthStatus.HEALTHY: 1.0,
    HealthStatus.UNKNOWN: 1.0,
    HealthStatus.DEGRADED: 2.0,
    HealthStatus.FAILING: 4.0,
    HealthStatus.OFFLINE: 12.0,
}


async def find_due_sources(
    session: AsyncSession,
    *,
    limit: int = 50,
    include_inactive: bool = False,
    respect_lifecycle: bool = True,
) -> Sequence[MunicipalitySource]:
    """Return sources whose next crawl is due, highest priority first.

    ``include_inactive`` exists for manual, operator-initiated runs (``--source``
    on the CLI): a human who says "crawl this one now" means it. The scheduler
    never passes it, and both paths still refuse a paused source — pausing is a
    statement about the source, not about who is asking.
    """
    now = utcnow()
    stmt = select(MunicipalitySource)
    if not include_inactive:
        stmt = stmt.where(MunicipalitySource.active.is_(True))
    stmt = stmt.order_by(
        MunicipalitySource.priority.asc(), MunicipalitySource.last_run_at.asc().nulls_first()
    ).limit(limit * 4)

    candidates = (await session.execute(stmt)).scalars().all()
    due: list[MunicipalitySource] = []
    skipped_lifecycle = 0
    for source in candidates:
        if not is_due(source, now=now, respect_lifecycle=respect_lifecycle):
            if respect_lifecycle and not _schedulable_lifecycle(source):
                skipped_lifecycle += 1
            continue
        due.append(source)
        if len(due) >= limit:
            break
    logger.info(
        "discovery.due_sources",
        candidates=len(candidates),
        due=len(due),
        skipped_not_activated=skipped_lifecycle,
    )
    return due


def _schedulable_lifecycle(source: MunicipalitySource) -> bool:
    lifecycle = SourceLifecycle.parse(source.lifecycle_status)
    return bool(lifecycle and lifecycle.schedulable)


def is_due(source: MunicipalitySource, *, now=None, respect_lifecycle: bool = True) -> bool:
    """Whether a source should run now.

    Order matters and each test answers a different question:

    * paused — an explicit human stop, overrides everything;
    * lifecycle — the source was never activated (or was retired), so the
      scheduler must not touch it even though ``active`` is true;
    * crawl interval × health backoff — the ordinary throttle.
    """
    moment = now or utcnow()
    if source.paused_at is not None:
        return False
    if respect_lifecycle and not _schedulable_lifecycle(source):
        return False
    if source.last_run_at is None:
        return True
    multiplier = BACKOFF_MULTIPLIER.get(HealthStatus.parse(source.health_status), 1.0)
    interval = timedelta(minutes=source.crawl_frequency_minutes * multiplier)
    return source.last_run_at + interval <= moment


async def find_sources_needing_attention(
    session: AsyncSession, *, min_consecutive_failures: int = 3, limit: int = 100
) -> Sequence[MunicipalitySource]:
    """Sources whose health indicates an engineering problem."""
    stmt = (
        select(MunicipalitySource)
        .where(
            or_(
                MunicipalitySource.consecutive_failures >= min_consecutive_failures,
                MunicipalitySource.health_status.in_(
                    [str(HealthStatus.FAILING), str(HealthStatus.OFFLINE)]
                ),
            )
        )
        .order_by(MunicipalitySource.consecutive_failures.desc())
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def discover_targets(
    connector: ProcurementConnector, source: SourceContext
) -> Sequence[DiscoveryTarget]:
    """Ask a connector which targets it will fetch (observable planning step)."""
    targets = await connector.discover(source)
    logger.info(
        "discovery.targets",
        source_id=source.id,
        connector=connector.key,
        target_count=len(targets),
    )
    return targets
