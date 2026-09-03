"""Source registry.

A *source* is a place procurement information is published. It is modelled
independently from the *connector* that knows how to read it, so a
municipality can change its website technology by editing configuration only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import (
    ConnectorType,
    HealthStatus,
    JobStatus,
    ProcurementScope,
    SourceLifecycle,
    SourceType,
    VerificationStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, erased at runtime
    from app.db.models.geography import Municipality, Province


class SourceConnector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered connector implementation available to the ingestion engine.

    Rows mirror the in-process connector registry (``app.connectors.registry``)
    so operators can inspect available implementations through the API.
    """

    __tablename__ = "source_connectors"

    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: JSON-schema-ish description of the ``config`` keys the connector accepts.
    config_schema: Mapped[dict | None] = mapped_column(nullable=True)
    requires_browser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: ``False`` for connectors whose external contract is unverified (e.g. the
    #: eTender OCDS connector until its live API has been confirmed).
    production_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    #: Operator-visible caveat, e.g. "live endpoint contract UNVERIFIED".
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MunicipalitySource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A concrete, configured procurement source.

    Despite the historical table name this represents *any* public-sector
    source (national, provincial, entity or municipal); ``municipality_id`` and
    ``province_id`` are optional scope pointers.
    """

    __tablename__ = "municipality_sources"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_municipality_sources_slug"),
        Index("ix_municipality_sources_active_priority", "active", "priority"),
        Index("ix_municipality_sources_health_status", "health_status"),
        # The claim query filters on eligibility and orders by priority; without this
        # it is a sequential scan of every source on every scheduler tick.
        Index("ix_municipality_sources_claim_due", "active", "next_run_at"),
        CheckConstraint("priority >= 0 AND priority <= 1000", name="priority_range"),
        CheckConstraint("crawl_frequency_minutes >= 5", name="crawl_frequency_min"),
        CheckConstraint("rate_limit_per_minute > 0", name="rate_limit_positive"),
        # A claim must be expiring: a row that names a job but carries no lease would
        # stay unschedulable forever if that job died. Enforced in the database because
        # the code that sets one sets the other, and a future caller that forgets is a
        # bug that would otherwise present as "this source stopped crawling".
        CheckConstraint(
            "claim_job_id IS NULL OR claim_expires_at IS NOT NULL", name="claim_has_lease"
        ),
        # The lifecycle is a closed set: an operator cannot invent a state that
        # the scheduler does not understand.
        CheckConstraint(
            "lifecycle_status IN ('DISCOVERED','PENDING_VERIFICATION','VERIFIED','ACTIVE',"
            "'DEGRADED','PAUSED','DISABLED')",
            name="lifecycle_status_known",
        ),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED','PASSED','PASSED_WITH_WARNINGS','FAILED')",
            name="verification_status_known",
        ),
        # A *passed* verification must carry the timestamp of the run that
        # established it, so "PASSED" in an API response can always be dated and
        # re-checked. Both passing statuses are covered: an earlier version of
        # this constraint named only PASSED, which let a source be verified with
        # warnings and carry no date at all — and it pointed at ``verified_at``
        # (the human confirmation stamp), so recording a passing automated run
        # was an INSERT-time violation.
        #
        # Deliberately absent: a constraint tying ``lifecycle_status='ACTIVE'`` to
        # a passing verification. Re-verifying an active source that has since
        # broken must be able to record ``FAILED``; a cross-column CHECK would
        # reject that write and hide the only evidence that matters. The
        # application refuses *activation* without a pass instead
        # (``SourceVerificationService.set_lifecycle``).
        CheckConstraint(
            "verification_status NOT IN ('PASSED', 'PASSED_WITH_WARNINGS') "
            "OR verification_at IS NOT NULL",
            name="passed_verification_is_dated",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(220), nullable=False)
    organization: Mapped[str] = mapped_column(String(240), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(40), nullable=False, default=SourceType.MUNICIPAL_WEBSITE, index=True
    )
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    procurement_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ProcurementScope.UNKNOWN
    )
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("municipalities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    province_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("provinces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    connector_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ConnectorType.HTML
    )
    connector_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Data-driven connector configuration (listing paths, selectors, ...).
    config: Mapped[dict | None] = mapped_column(nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    crawl_frequency_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=360)

    # -- health -----------------------------------------------------------
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_response_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HealthStatus.UNKNOWN
    )

    # -- scheduling / claiming --------------------------------------------
    #: When this source may next be claimed by a scheduler. The claim path sets it
    #: forward (crawl interval x health backoff) inside the same transaction that
    #: creates the job row, which is what makes "two scheduler replicas enqueue the
    #: same source" impossible rather than merely unlikely. ``NULL`` means eligible
    #: now, so a freshly registered or manually-run source needs no special case.
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: End of the current claim's lease. A claim without an expiry is a locked
    #: source: if the worker is killed between claiming and running, nothing would
    #: ever crawl it again. Reconciliation reclaims rows whose lease has passed.
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: The ``ingestion_jobs`` row holding the lease (not a foreign key on purpose:
    #: ``ingestion_jobs.source_id`` already points here, and a second edge between
    #: the same two tables would make table ordering circular for ``create_all``).
    claim_job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    # -- politeness -------------------------------------------------------
    robots_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="RESPECT")
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # -- lifecycle --------------------------------------------------------
    #: Operator-facing lifecycle state, distinct from ``health_status`` (which
    #: only reflects recent run results). See :class:`SourceLifecycle`.
    lifecycle_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=SourceLifecycle.DISCOVERED,
        server_default=str(SourceLifecycle.DISCOVERED),
        index=True,
    )
    #: Result of the verification procedure (``scripts/verify_source.py``).
    #: ``UNVERIFIED`` is the only state a freshly imported source may have.
    verification_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
        server_default=str(VerificationStatus.UNVERIFIED),
        index=True,
    )
    #: Machine-readable outcome of the most recent verification run.
    verification_result: Mapped[dict | None] = mapped_column(nullable=True)
    verification_at: Mapped[datetime | None] = mapped_column(nullable=True)
    verification_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: HTTP status observed by the last verification probe (``None`` = never probed).
    verification_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Free-form operator notes: verification date, known limitations, ...
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When a human last confirmed this source definition is correct.
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: Pause/resume bookkeeping so ``PAUSED`` is auditable rather than implicit.
    paused_at: Mapped[datetime | None] = mapped_column(nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    municipality: Mapped[Municipality | None] = relationship(  # noqa: F821
        back_populates="sources", lazy="joined"
    )
    province: Mapped[Province | None] = relationship(lazy="joined")  # noqa: F821
    runs: Mapped[list[SourceRun]] = relationship(
        back_populates="source", cascade="all, delete-orphan", lazy="noload"
    )


class SourceRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of one source through the ingestion pipeline."""

    __tablename__ = "source_runs"
    __table_args__ = (
        Index("ix_source_runs_source_id_started_at", "source_id", "started_at"),
        CheckConstraint("items_found >= 0", name="items_found_non_negative"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("municipality_sources.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.RUNNING)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    documents_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict | None] = mapped_column(nullable=True)

    source: Mapped[MunicipalitySource] = relationship(back_populates="runs", lazy="joined")
