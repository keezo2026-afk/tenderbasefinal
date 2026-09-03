"""Procurement opportunity schemas — the public API contract.

``NormalizedOpportunity`` is the *internal* canonical record produced by the
ingestion pipeline; ``TenderRead``/``TenderDetail`` are the *external* API
representations. Keeping them separate means the database and the pipeline can
evolve without breaking API clients.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import (
    DataQuality,
    OpportunityStatus,
    ProcurementType,
)
from app.schemas.common import TenderBaseModel
from app.schemas.document import DocumentCandidate, DocumentRead
from app.schemas.municipality import MunicipalityRef, ProvinceRef
from app.schemas.source import SourceRef

MAX_TITLE_LENGTH = 2000


class ContactRead(TenderBaseModel):
    id: UUID
    name: str | None = None
    role: str | None = None
    organization: str | None = None
    email: str | None = None
    phone: str | None = None


class TenderRead(TenderBaseModel):
    """List representation of a procurement opportunity."""

    id: UUID
    reference_number: str | None = None
    title: str
    procurement_type: ProcurementType
    status: OpportunityStatus
    organization: str | None = None
    municipality: MunicipalityRef | None = None
    province: ProvinceRef | None = None
    source: SourceRef | None = None
    published_at: datetime | None = None
    closing_at: datetime | None = None
    estimated_value: Decimal | None = None
    currency: str | None = None
    source_url: str
    data_quality: DataQuality = DataQuality.NEEDS_REVIEW
    confidence: float = 1.0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    is_test_fixture: bool = Field(
        default=False,
        description="True for clearly-marked development/test fixture records",
    )


class TenderDetail(TenderRead):
    """Full representation, including submission, briefing and documents."""

    external_id: str | None = None
    description: str | None = None
    submission_method: str | None = None
    submission_url: str | None = None
    submission_address: str | None = None
    briefing_required: bool | None = None
    briefing_compulsory: bool | None = None
    briefing_date: datetime | None = None
    briefing_location: str | None = None
    contact: ContactRead | None = None
    canonical_url: str | None = None
    content_hash: str
    version: int = 1
    quality_issues: dict[str, Any] | None = None
    source_timezone: str | None = None
    raw_dates: dict[str, Any] | None = None
    documents: list[DocumentRead] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenderFilter(BaseModel):
    """Typed query filters for ``GET /api/v1/tenders``."""

    model_config = ConfigDict(extra="forbid")

    province: str | None = Field(default=None, description="Province name, code or slug")
    district: str | None = Field(default=None, description="District name, code or slug")
    municipality: str | None = Field(default=None, description="Municipality name, code or slug")
    municipality_id: UUID | None = None
    source_id: UUID | None = None
    type: ProcurementType | None = Field(default=None, description="Procurement type")
    status: OpportunityStatus | None = None
    category: str | None = Field(default=None, description="Category slug")
    reference_number: str | None = Field(default=None, max_length=200)
    published_after: date | datetime | None = None
    published_before: date | datetime | None = None
    closing_after: date | datetime | None = None
    closing_before: date | datetime | None = None
    min_value: Annotated[Decimal | None, Field(ge=0)] = None
    max_value: Annotated[Decimal | None, Field(ge=0)] = None
    data_quality: DataQuality | None = None
    include_test_fixtures: bool = Field(
        default=False, description="Include records flagged as development fixtures"
    )
    q: str | None = Field(default=None, max_length=300, description="Free-text query")
    sort: str = Field(
        default="-published_at",
        description="Sort key: published_at, closing_at, created_at, last_seen_at, relevance "
        "(prefix with '-' for descending)",
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        if (
            self.published_after
            and self.published_before
            and self.published_after > self.published_before
        ):
            raise ValueError("published_after must be earlier than published_before")
        if self.closing_after and self.closing_before and self.closing_after > self.closing_before:
            raise ValueError("closing_after must be earlier than closing_before")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")
        return self

    @field_validator("sort")
    @classmethod
    def _check_sort(cls, value: str) -> str:
        allowed = {
            "published_at",
            "closing_at",
            "created_at",
            "last_seen_at",
            "title",
            "relevance",
        }
        if value.lstrip("-") not in allowed:
            raise ValueError(f"sort must be one of {sorted(allowed)} (optionally '-' prefixed)")
        return value


class SearchQuery(TenderFilter):
    """Query model for ``GET /api/v1/search`` — ``q`` is required."""

    q: str = Field(min_length=2, max_length=300, description="Full-text query")
    sort: str = "relevance"


class SearchHit(TenderRead):
    """A search result with its relevance score and highlight snippet."""

    score: float | None = None
    snippet: str | None = None


# --- Internal canonical record -------------------------------------------


class NormalizedOpportunity(BaseModel):
    """Canonical record emitted by the normalizer and consumed by persistence.

    This is deliberately permissive about *missing* data (``None`` is always
    preferable to fabricated values) and strict about *shape*.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    reference_number: str | None = None
    reference_number_normalized: str | None = None

    title: str
    description: str | None = None
    procurement_type: ProcurementType = ProcurementType.OTHER
    status: OpportunityStatus = OpportunityStatus.UNKNOWN

    organization: str | None = None
    municipality_id: UUID | None = None
    province_id: UUID | None = None
    source_id: UUID

    published_at: datetime | None = None
    closing_at: datetime | None = None
    source_timezone: str | None = None
    raw_dates: dict[str, Any] = Field(default_factory=dict)

    estimated_value: Decimal | None = None
    currency: str | None = None

    submission_method: str | None = None
    submission_url: str | None = None
    submission_address: str | None = None

    briefing_required: bool | None = None
    briefing_compulsory: bool | None = None
    briefing_date: datetime | None = None
    briefing_location: str | None = None

    contact: dict[str, Any] | None = None

    source_url: str
    canonical_url: str | None = None
    content_hash: str = ""
    fingerprint: str = ""

    raw_payload: dict[str, Any] | None = None
    raw_payload_key: str | None = None
    parser_metadata: dict[str, Any] = Field(default_factory=dict)

    documents: list[DocumentCandidate] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    data_quality: DataQuality = DataQuality.NEEDS_REVIEW
    quality_issues: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    is_test_fixture: bool = False

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned[:MAX_TITLE_LENGTH]

    @field_validator("currency")
    @classmethod
    def _currency_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            return None
        return code

    def hashable_payload(self) -> dict[str, Any]:
        """The canonical subset used for content hashing and versioning."""
        return {
            "reference_number": self.reference_number_normalized or self.reference_number,
            "title": self.title,
            "description": self.description,
            "procurement_type": str(self.procurement_type),
            "status": str(self.status),
            "organization": self.organization,
            "published_at": self.published_at,
            "closing_at": self.closing_at,
            "estimated_value": self.estimated_value,
            "currency": self.currency,
            "submission_method": self.submission_method,
            "submission_url": self.submission_url,
            "briefing_required": self.briefing_required,
            "briefing_date": self.briefing_date,
            "briefing_location": self.briefing_location,
            "source_url": self.source_url,
            "documents": sorted(doc.source_url for doc in self.documents),
        }

    def fingerprint_payload(self) -> dict[str, Any]:
        """Aggressively normalized identity fields (layer-3 dedup)."""
        return {
            "title": self.title,
            "organization": self.organization,
            "closing_at": self.closing_at,
            "procurement_type": str(self.procurement_type),
        }
