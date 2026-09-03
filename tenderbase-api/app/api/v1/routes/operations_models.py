"""Response models for the operational endpoints.

Kept next to the routes that use them rather than in ``app/schemas``: these are
operator-facing diagnostic shapes, not the public data contract, and they must
never be mistaken for a stable API surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import TenderBaseModel


class RunErrorSample(TenderBaseModel):
    stage: str
    code: str
    message: str
    url: str | None = None
    retryable: bool = False
    occurred_at: datetime | None = None


class RunReportRead(TenderBaseModel):
    """The complete outcome of one source run."""

    run_id: str = Field(description="Empty string when the source has never run")
    source_id: str
    source_name: str
    source_slug: str
    base_url: str
    connector_key: str | None = None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    items_found: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    documents_found: int = 0
    uncertain_duplicates: int = 0
    error_count: int = 0
    http_status: int | None = None
    first_error: str | None = None
    errors: list[RunErrorSample] = Field(default_factory=list)
    verdict: str = "UNKNOWN"
    verdict_reason: str | None = None


class SourceHealthSnapshot(TenderBaseModel):
    """Operational state of a source that needs attention."""

    id: str
    name: str
    slug: str
    connector_key: str | None = None
    lifecycle_status: str
    health_status: str
    verification_status: str
    consecutive_failures: int = 0
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_http_status: int | None = None


class DuplicateMatch(TenderBaseModel):
    existing_id: str | None = None
    existing_title: str | None = None
    layer: str | None = None
    confidence: float | None = None
    reason: str | None = None


class DuplicateCandidate(TenderBaseModel):
    """An ingested record held back from auto-merging."""

    opportunity_id: str
    title: str
    reference_number: str | None = None
    source_id: str
    matches: list[DuplicateMatch] = Field(default_factory=list)

    model_config = {"from_attributes": True, "extra": "ignore", "protected_namespaces": ()}


class VerificationCheckRead(TenderBaseModel):
    name: str
    status: str
    detail: str
    required: bool = True
    duration_ms: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerificationReportRead(TenderBaseModel):
    status: str
    checked_at: str
    duration_ms: int
    base_url: str
    connector_key: str | None = None
    http_status: int | None = None
    summary: str
    items_discovered: int = 0
    documents_found: int = 0
    checks: list[VerificationCheckRead] = Field(default_factory=list)


class RecoveryActionRead(TenderBaseModel):
    """One repair the reconciliation pass made (or would have made, in dry run)."""

    action: str
    job_id: str | None = None
    source_id: str | None = None
    detail: str = ""


class RecoveryReportRead(TenderBaseModel):
    """What one reconciliation pass found and changed.

    Idempotent by construction: running the same pass twice reports
    ``actions_count=0`` the second time, which is the response an operator should
    expect when they re-run it to check.
    """

    started_at: str
    dry_run: bool = False
    reenqueue_enabled: bool = True
    actions_count: int = 0
    #: Action name → how many times it was applied.
    counts: dict[str, int] = Field(default_factory=dict)
    #: The state the pass started from (job counts by status, claimed sources, open
    #: runs), so "nothing to do" is distinguishable from "nothing to see".
    checked: dict[str, int] = Field(default_factory=dict)
    source_freshness: dict[str, int] = Field(default_factory=dict)
    #: A bounded sample; ``counts`` is the aggregate.
    actions: list[RecoveryActionRead] = Field(default_factory=list)


class SourceFreshnessRead(TenderBaseModel):
    """How out of date one source's data is, against the configured thresholds."""

    source_id: str
    slug: str
    name: str
    active: bool
    lifecycle_status: str
    #: FRESH | AGING | STALE | NEVER_RUN | PAUSED | NOT_ACTIVE
    freshness_state: str
    last_run_at: str | None = None
    last_success_at: str | None = None
    hours_since_success: float | None = None
    next_run_at: str | None = None
    #: Set while a worker owns this source. A *past* timestamp here with no live job is
    #: a stale lease the next reconciliation pass will clear.
    claim_expires_at: str | None = None
    health_status: str
    consecutive_failures: int = 0
