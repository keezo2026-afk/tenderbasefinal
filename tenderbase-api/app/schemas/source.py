"""Source registry schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.enums import (
    ConnectorType,
    HealthStatus,
    JobStatus,
    ProcurementScope,
    SourceLifecycle,
    SourceType,
    VerificationStatus,
)
from app.schemas.common import TenderBaseModel
from app.schemas.municipality import MunicipalityRef, ProvinceRef


class SourceRef(TenderBaseModel):
    """Compact source reference embedded in tender responses."""

    id: UUID
    name: str
    source_type: SourceType = SourceType.MUNICIPAL_WEBSITE


class SourceHealth(TenderBaseModel):
    """Operational health of a source."""

    health_status: HealthStatus = HealthStatus.UNKNOWN
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    average_response_time_ms: float | None = None
    last_http_status: int | None = None


class SourceRead(SourceRef):
    slug: str
    organization: str
    base_url: str
    procurement_scope: ProcurementScope = ProcurementScope.UNKNOWN
    connector_type: ConnectorType = ConnectorType.HTML
    connector_key: str | None = None
    municipality: MunicipalityRef | None = None
    province: ProvinceRef | None = None
    active: bool = True
    priority: int = 100
    crawl_frequency_minutes: int = 360
    robots_policy: str = "RESPECT"
    rate_limit_per_minute: int = 30
    notes: str | None = None
    verified_at: datetime | None = Field(
        default=None,
        description=(
            "When a human last verified this source definition. Distinct from "
            "`verification_at`, which records when the automated procedure ran."
        ),
    )
    health: SourceHealth | None = None

    # -- lifecycle (operator-facing state machine) ------------------------
    lifecycle_status: SourceLifecycle = SourceLifecycle.DISCOVERED
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_at: datetime | None = None
    verification_duration_ms: int | None = None
    verification_http_status: int | None = None
    paused_at: datetime | None = None
    paused_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SourceFilter(TenderBaseModel):
    """Query filters for the source collection."""

    source_type: SourceType | None = None
    connector_type: ConnectorType | None = None
    health_status: HealthStatus | None = None
    province: str | None = None
    municipality_id: UUID | None = None
    active: bool | None = None
    lifecycle_status: SourceLifecycle | None = None
    verification_status: VerificationStatus | None = None
    q: str | None = Field(default=None, max_length=200)


class SourceRunRead(TenderBaseModel):
    """One pipeline execution of a source."""

    id: UUID
    source_id: UUID
    job_id: UUID | None = None
    status: JobStatus = JobStatus.QUEUED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    items_found: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    documents_found: int = 0
    http_status: int | None = None
    error_message: str | None = None


class SourceRunReport(SourceRunRead):
    """A run plus the counters an operator needs to answer "did it work?"."""

    documents_found: int = 0
    stats: dict[str, Any] | None = None
    source_name: str | None = None
    source_slug: str | None = None
    base_url: str | None = None
    connector_key: str | None = None
    error_count: int = 0
    uncertain_duplicates: int = 0
    outcome: str | None = None


class ConnectorRead(TenderBaseModel):
    """A connector implementation available to the ingestion engine."""

    key: str
    name: str
    connector_type: ConnectorType
    version: str = "0.1.0"
    description: str | None = None
    requires_browser: bool = False
    config_schema: dict | None = None
    #: ``False`` for connectors whose external contract is unverified or whose
    #: runtime dependencies are optional. Honest labelling beats silence.
    production_ready: bool = True
    status_note: str | None = None


class SourceDefinition(TenderBaseModel):
    """Data-driven source definition used by seeding/import scripts.

    This is the on-disk contract for ``scripts/import_sources.py``; it never
    invents URLs — operators supply verified values.
    """

    name: str
    organization: str
    source_type: SourceType
    connector_type: ConnectorType
    base_url: str
    slug: str | None = None
    procurement_scope: ProcurementScope = ProcurementScope.UNKNOWN
    municipality_code: str | None = None
    province_code: str | None = None
    connector_key: str | None = None
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 100
    crawl_frequency_minutes: int = 360
    rate_limit_per_minute: int = 30
    robots_policy: str = "RESPECT"
    notes: str | None = None
    verified_at: datetime | None = None
