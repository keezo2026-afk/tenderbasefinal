"""Ingestion jobs and per-item ingestion errors."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import ErrorStage, JobStatus, JobTrigger


class IngestionJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A unit of ingestion work — usually "run source X once"."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_ingestion_jobs_status_scheduled_for", "status", "scheduled_for"),
        Index("ix_ingestion_jobs_source_id_created_at", "source_id", "created_at"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    )

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("municipality_sources.id", ondelete="CASCADE"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(40), nullable=False, default="SOURCE_INGEST")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default=JobTrigger.MANUAL)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    scheduled_for: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(nullable=True)
    result: Mapped[dict | None] = mapped_column(nullable=True)
    #: External queue identifier (ARQ job id) when dispatched to a worker.
    queue_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    errors: Mapped[list[IngestionError]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="noload"
    )


class IngestionError(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single recorded ingestion failure.

    Errors are recorded per item/stage so that one broken page — or one broken
    municipal website — never aborts the rest of the pipeline.
    """

    __tablename__ = "ingestion_errors"
    __table_args__ = (
        Index("ix_ingestion_errors_job_id_created_at", "job_id", "created_at"),
        Index("ix_ingestion_errors_source_id_stage", "source_id", "stage"),
    )

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("municipality_sources.id", ondelete="SET NULL"), nullable=True
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("source_runs.id", ondelete="CASCADE"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default=ErrorStage.UNKNOWN)
    error_code: Mapped[str] = mapped_column(String(60), nullable=False, default="UNKNOWN")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool] = mapped_column(nullable=False, default=False)
    context: Mapped[dict | None] = mapped_column(nullable=True)

    job: Mapped[IngestionJob | None] = relationship(back_populates="errors")
