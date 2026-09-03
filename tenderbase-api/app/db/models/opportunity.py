"""The canonical procurement opportunity spine, its versions and its events."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import DataQuality, EventType, OpportunityStatus, ProcurementType

if TYPE_CHECKING:  # pragma: no cover - typing only, erased at runtime
    from app.db.models.category import OpportunityCategory
    from app.db.models.document import Document
    from app.db.models.geography import Municipality, Province
    from app.db.models.source import MunicipalitySource


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A procurement contact person / desk published by a source."""

    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_email", "email"),
        UniqueConstraint("fingerprint", name="uq_contacts_fingerprint"),
    )

    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str | None] = mapped_column(String(160), nullable=True)
    organization: Mapped[str | None] = mapped_column(String(240), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fax: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Stable hash of the normalized contact fields, used for deduplication.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class ProcurementOpportunity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The canonical, normalized procurement opportunity."""

    __tablename__ = "procurement_opportunities"
    __table_args__ = (
        # Layer 1 deduplication key: an issuer never reuses a reference number.
        UniqueConstraint(
            "municipality_id", "reference_number", name="uq_opportunity_municipality_reference"
        ),
        UniqueConstraint("source_id", "external_id", name="uq_opportunity_source_external"),
        UniqueConstraint("fingerprint", name="uq_opportunity_fingerprint"),
        Index("ix_opportunities_status_closing_at", "status", "closing_at"),
        Index("ix_opportunities_published_at", "published_at"),
        Index("ix_opportunities_municipality_status", "municipality_id", "status"),
        Index("ix_opportunities_province_status", "province_id", "status"),
        Index("ix_opportunities_source_last_seen", "source_id", "last_seen_at"),
        Index("ix_opportunities_content_hash", "content_hash"),
        Index("ix_opportunities_procurement_type", "procurement_type"),
        CheckConstraint(
            "estimated_value IS NULL OR estimated_value >= 0", name="estimated_value_non_negative"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    # -- identity ---------------------------------------------------------
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    #: Reference number reduced to comparable form (uppercase, no separators).
    reference_number_normalized: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # -- content ----------------------------------------------------------
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    procurement_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProcurementType.OTHER
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OpportunityStatus.UNKNOWN, index=True
    )

    # -- provenance / scope ----------------------------------------------
    organization: Mapped[str | None] = mapped_column(String(240), nullable=True)
    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("municipalities.id", ondelete="SET NULL"), nullable=True
    )
    province_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("provinces.id", ondelete="SET NULL"), nullable=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("municipality_sources.id", ondelete="RESTRICT"), nullable=False
    )

    # -- dates (stored UTC, source timezone preserved) --------------------
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closing_at: Mapped[datetime | None] = mapped_column(nullable=True)
    source_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Raw, unparsed date strings exactly as published (audit trail).
    raw_dates: Mapped[dict | None] = mapped_column(nullable=True)

    # -- commercials ------------------------------------------------------
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # -- submission -------------------------------------------------------
    submission_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submission_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- briefing ---------------------------------------------------------
    briefing_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    briefing_compulsory: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    briefing_date: Mapped[datetime | None] = mapped_column(nullable=True)
    briefing_location: Mapped[str | None] = mapped_column(Text, nullable=True)

    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )

    # -- traceability -----------------------------------------------------
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- raw preservation --------------------------------------------------
    #: Small raw payloads inline; large ones live in the blob store and only
    #: their storage key is kept here (see ``app/documents/storage.py``).
    raw_payload: Mapped[dict | None] = mapped_column(nullable=True)
    raw_payload_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    parser_metadata: Mapped[dict | None] = mapped_column(nullable=True)

    # -- quality ----------------------------------------------------------
    data_quality: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataQuality.NEEDS_REVIEW
    )
    quality_issues: Mapped[dict | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # -- lifecycle --------------------------------------------------------
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Marked when a fixture/dev record is loaded — never real procurement data.
    is_test_fixture: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- relationships ----------------------------------------------------
    municipality: Mapped[Municipality | None] = relationship(lazy="joined")  # noqa: F821
    province: Mapped[Province | None] = relationship(lazy="joined")  # noqa: F821
    source: Mapped[MunicipalitySource] = relationship(lazy="joined")  # noqa: F821
    contact: Mapped[Contact | None] = relationship(lazy="joined")
    versions: Mapped[list[OpportunityVersion]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="noload"
    )
    events: Mapped[list[OpportunityEvent]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="noload"
    )
    documents: Mapped[list[Document]] = relationship(  # noqa: F821
        back_populates="opportunity", cascade="all, delete-orphan", lazy="noload"
    )
    categories: Mapped[list[OpportunityCategory]] = relationship(  # noqa: F821
        back_populates="opportunity", cascade="all, delete-orphan", lazy="noload"
    )


class OpportunityVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An immutable snapshot of an opportunity at a point in time."""

    __tablename__ = "opportunity_versions"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "version", name="uq_opportunity_versions_version"),
        Index("ix_opportunity_versions_opportunity_created", "opportunity_id", "created_at"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("procurement_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Full canonical snapshot of the record at this version.
    snapshot: Mapped[dict] = mapped_column(nullable=False)
    #: Field-level diff against the previous version.
    changed_fields: Mapped[dict | None] = mapped_column(nullable=True)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("source_runs.id", ondelete="SET NULL"), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(nullable=False)

    opportunity: Mapped[ProcurementOpportunity] = relationship(back_populates="versions")


class OpportunityEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A semantic change event on an opportunity (the public change feed)."""

    __tablename__ = "opportunity_events"
    __table_args__ = (
        Index("ix_opportunity_events_opportunity_occurred", "opportunity_id", "occurred_at"),
        Index("ix_opportunity_events_type_occurred", "event_type", "occurred_at"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("procurement_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default=EventType.OTHER)
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("opportunity_versions.id", ondelete="SET NULL"), nullable=True
    )
    field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    previous_value: Mapped[dict | None] = mapped_column(nullable=True)
    new_value: Mapped[dict | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, index=True)

    opportunity: Mapped[ProcurementOpportunity] = relationship(back_populates="events")
