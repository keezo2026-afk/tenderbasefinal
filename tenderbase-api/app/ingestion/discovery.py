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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.connectors.base import DiscoveryTarget, ProcurementConnector, SourceContext
from app.db.models.ingestion import IngestionJob
from app.db.models.source import MunicipalitySource
from app.enums import HealthStatus, JobStatus, JobTrigger, SourceLifecycle
from app.logging import get_logger
from app.utils.dates import ensure_utc, utcnow

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

    A *read-only* view: it takes no claim, so it is what an operator's manual run and
    the "what would run next" endpoints use. The worker uses :func:`claim_due_sources`,
    which is the same predicate plus the transaction that makes it exclusive.


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


#: Lifecycle states a scheduler may claim. Derived from the enum, so adding a state
#: there cannot silently leave this list stale.
SCHEDULABLE_LIFECYCLES: tuple[str, ...] = tuple(
    sorted(str(member) for member in SourceLifecycle if member.schedulable)
)


def _stored_utc(value: datetime | None) -> datetime | None:
    """Read a timestamp stored by this application back as an aware UTC value.

    SQLite has no timestamp-with-offset type: the driver writes the UTC wall clock and
    hands back a *naive* datetime, while :func:`utcnow` and every horizon computed here
    are aware. Comparing the two is a ``TypeError``, which is why every comparison against
    a stored column goes through this. PostgreSQL returns aware values, where this is a
    no-op; the ``assume_timezone`` is UTC rather than the source-timezone default
    because the column holds *our* clock, not a publisher's local time.
    """
    return ensure_utc(value, assume_timezone="UTC")


def next_eligible_run(
    source: MunicipalitySource, *, now: datetime | None = None, from_last_run: bool = False
) -> datetime:
    """When this source may next be claimed: crawl interval × health backoff.

    The single place that arithmetic lives, used both when a claim is taken and when a
    run finishes, so the two cannot drift into disagreeing about the schedule.
    ``from_last_run`` anchors the horizon on the run that just ended rather than on
    ``now`` — a 40-minute crawl on a 60-minute interval should leave 20 minutes, not a
    fresh hour.
    """
    moment = now or utcnow()
    multiplier = BACKOFF_MULTIPLIER.get(HealthStatus.parse(source.health_status), 1.0)
    interval = timedelta(minutes=source.crawl_frequency_minutes * multiplier)
    last_run = _stored_utc(source.last_run_at)
    if from_last_run and last_run is not None:
        return last_run + interval
    return moment + interval


def is_due(source: MunicipalitySource, *, now=None, respect_lifecycle: bool = True) -> bool:
    """Whether a source should run now.

    Order matters and each test answers a different question:

    * paused — an explicit human stop, overrides everything;
    * lifecycle — the source was never activated (or was retired), so the
      scheduler must not touch it even though ``active`` is true;
    * the claim horizon (``next_run_at``) — what the scheduler promised itself;
    * a live lease — somebody is already working on it;
    * crawl interval × health backoff — the throttle for rows that predate any
      claim (a source imported by hand, or a database restored from before
      claiming existed).
    """
    moment = now or utcnow()
    if source.paused_at is not None:
        return False
    if respect_lifecycle and not _schedulable_lifecycle(source):
        return False
    horizon = _stored_utc(source.next_run_at)
    if horizon is not None and horizon > moment:
        return False
    if _lease_held(source, moment):
        return False
    last_run = _stored_utc(source.last_run_at)
    if last_run is None:
        return True
    multiplier = BACKOFF_MULTIPLIER.get(HealthStatus.parse(source.health_status), 1.0)
    interval = timedelta(minutes=source.crawl_frequency_minutes * multiplier)
    return last_run + interval <= moment


def _lease_held(source: MunicipalitySource, moment: datetime) -> bool:
    expires = _stored_utc(source.claim_expires_at)
    return expires is not None and expires > moment


@dataclass(frozen=True, slots=True)
class SourceClaim:
    """A source this process now owns, and the job row that carries the ownership."""

    source: MunicipalitySource
    job: IngestionJob

    @property
    def source_id(self) -> str:
        return str(self.source.id)


def claim_conditions(now: datetime) -> list[Any]:
    """The eligibility predicate, in SQL, matching :func:`is_due`.

    Kept as a function of ``now`` (rather than inline in a query) because the claim
    path and the read-only "what is due" path must agree, and a difference between them
    shows up as sources that look due forever or never.
    """
    return [
        MunicipalitySource.active.is_(True),
        MunicipalitySource.paused_at.is_(None),
        MunicipalitySource.lifecycle_status.in_(SCHEDULABLE_LIFECYCLES),
        or_(
            MunicipalitySource.next_run_at.is_(None),
            MunicipalitySource.next_run_at <= now,
        ),
        or_(
            MunicipalitySource.claim_expires_at.is_(None),
            MunicipalitySource.claim_expires_at <= now,
        ),
    ]


async def claim_due_sources(
    session: AsyncSession,
    *,
    limit: int = 25,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[SourceClaim]:
    """Take an atomically-exclusive claim on the sources that are due.

    Why this exists: with one scheduler replica, reading ``find_due_sources`` and then
    enqueueing is fine. With two — a second worker deployment, a rolling restart with an
    overlap, a cron tick that outlived its predecessor — both read the same due set
    before either wrote anything, and each created a job for the same source. The fix
    is not a Redis lock somebody hopes to reacquire: it is that *the row that decides
    eligibility also records the decision*, in one transaction.

    ``FOR UPDATE SKIP LOCKED`` (PostgreSQL) makes concurrent claimers take disjoint
    sets without waiting: a locked row is simply not a candidate, so a slow claimer
    never blocks a fast one, and no source is claimed twice. On SQLite the row lock is
    unavailable and the statement runs as a plain ``SELECT`` — correct for the single
    process that backend exists for (development, tests), and :func:`is_due` plus the
    unique queue id keep it from being a footgun.

    Enqueueing happens *after* this transaction commits. If the enqueue fails the caller
    releases the claim (``release_claim``) so the next tick can try again; if this
    process dies before that, the lease expires and reconciliation reclaims it. Either
    way a claim cannot become a permanent lock.
    """
    cfg = settings or get_settings()
    moment = now or utcnow()
    lease = timedelta(seconds=cfg.source_claim_lease_seconds)

    # Lock the ids, then load the rows. Locking the entity query directly looks equivalent
    # and is not: ``MunicipalitySource`` eager-loads its municipality/province through
    # LEFT OUTER JOINs, and PostgreSQL refuses ``FOR UPDATE`` over the nullable side of an
    # outer join ("FOR UPDATE cannot be applied to the nullable side of an outer join"),
    # which would make *every* claim fail on the database claiming exists for. Selecting
    # bare ids keeps the lock on one table, and the rows it locks are ours to read.
    id_stmt = (
        select(MunicipalitySource.id)
        .where(*claim_conditions(moment))
        .order_by(
            MunicipalitySource.priority.asc(),
            MunicipalitySource.last_run_at.asc().nulls_first(),
        )
        .limit(limit)
    )
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        id_stmt = id_stmt.with_for_update(skip_locked=True)

    claimed_ids = list((await session.execute(id_stmt)).scalars().all())
    if not claimed_ids:
        return []
    sources = list(
        (
            await session.execute(
                select(MunicipalitySource).where(MunicipalitySource.id.in_(claimed_ids))
            )
        )
        .scalars()
        .all()
    )
    # ``IN`` loses the ORDER BY, and the order is the point: priority decides who gets
    # the scarce worker slots when the batch is cut off by ``limit``.
    rank = {source_id: index for index, source_id in enumerate(claimed_ids)}
    sources.sort(key=lambda row: rank.get(row.id, len(rank)))
    claims: list[SourceClaim] = []
    for source in sources:
        job = IngestionJob(
            source_id=source.id,
            job_type="SOURCE_INGEST",
            status=str(JobStatus.QUEUED),
            trigger=str(JobTrigger.SCHEDULER),
            scheduled_for=moment,
        )
        session.add(job)
        await session.flush()

        source.next_run_at = next_eligible_run(source, now=moment)
        source.claim_expires_at = moment + lease
        source.claim_job_id = job.id
        job.payload = {**(job.payload or {}), "claimed_until": source.claim_expires_at.isoformat()}
        claims.append(SourceClaim(source=source, job=job))

    if claims:  # pragma: no branch - claimed_ids above guarantees at least one
        # The claims are only real once committed: the whole point is that another
        # process can observe them. Callers must commit before dispatching to Redis.
        logger.info(
            "discovery.sources_claimed",
            claimed=len(claims),
            lease_seconds=cfg.source_claim_lease_seconds,
        )
    return claims


async def release_claim(
    session: AsyncSession,
    *,
    source: MunicipalitySource,
    job_id: UUID | str | None = None,
    reschedule: bool = False,
    settings: Settings | None = None,
) -> bool:
    """Drop a source's lease, optionally pushing its next eligibility out.

    Only the holder may release: a late-finishing job from an *earlier* claim must not
    clear the lease of the claim that replaced it, or two workers end up crawling the
    same source with neither knowing. Returns whether this call released anything.
    """
    if job_id is not None and source.claim_job_id is not None:
        if str(source.claim_job_id) != str(job_id):
            logger.info(
                "discovery.claim_release_skipped",
                source_id=str(source.id),
                held_by=str(source.claim_job_id),
                attempted=str(job_id),
            )
            return False
    source.claim_job_id = None
    source.claim_expires_at = None
    if reschedule:
        # Anchor on the run that just finished, so a crawl that took 40 minutes of a
        # 60-minute interval leaves 20 minutes rather than a fresh hour.
        source.next_run_at = next_eligible_run(source, now=utcnow(), from_last_run=True)
    _ = settings  # accepted for symmetry with the claim path; the horizon needs no config
    await session.flush()
    return True


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
