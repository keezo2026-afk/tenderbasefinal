"""Validation engine.

Classifies a normalized record as ``VALID``, ``INCOMPLETE``, ``NEEDS_REVIEW``
or ``INVALID``. Missing *optional* fields never make a legitimate record
invalid — they only lower its completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.enums import DataQuality, OpportunityStatus, ProcurementType
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import utcnow
from app.utils.urls import is_http_url

MIN_TITLE_LENGTH = 8
MAX_FUTURE_YEARS = 10
MAX_PAST_YEARS = 25


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating one normalized opportunity."""

    quality: DataQuality
    issues: dict[str, list[str]] = field(default_factory=dict)
    completeness: float = 0.0

    @property
    def is_persistable(self) -> bool:
        """Invalid records are recorded as ingestion errors, not persisted."""
        return self.quality is not DataQuality.INVALID

    def add(self, field_name: str, message: str) -> None:
        self.issues.setdefault(field_name, []).append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "quality": str(self.quality),
            "completeness": self.completeness,
            "issues": self.issues,
        }


#: Fields contributing to the completeness score, with their weights.
COMPLETENESS_WEIGHTS: dict[str, float] = {
    "reference_number": 0.15,
    "description": 0.15,
    "published_at": 0.15,
    "closing_at": 0.20,
    "organization": 0.10,
    "documents": 0.15,
    "contact": 0.10,
}


class Validator:
    """Validates normalized opportunities before persistence."""

    def validate(self, record: NormalizedOpportunity) -> ValidationResult:
        result = ValidationResult(quality=DataQuality.VALID)
        now = utcnow()

        # --- hard requirements (INVALID) ---------------------------------
        title = (record.title or "").strip()
        if not title:
            result.add("title", "missing")
        elif len(title) < MIN_TITLE_LENGTH:
            result.add("title", f"shorter than {MIN_TITLE_LENGTH} characters")

        if not record.source_url or not is_http_url(record.source_url):
            result.add("source_url", "missing or not a valid http(s) URL")
        if record.source_id is None:
            result.add("source_id", "missing")

        hard_failure = bool(result.issues)

        # --- soft checks --------------------------------------------------
        if record.published_at and record.published_at > now + timedelta(days=2):
            result.add("published_at", "publication date is in the future")
        if record.published_at and record.published_at < now - timedelta(days=365 * MAX_PAST_YEARS):
            result.add("published_at", "publication date is implausibly old")
        if record.closing_at and record.closing_at > now + timedelta(days=365 * MAX_FUTURE_YEARS):
            result.add("closing_at", "closing date is implausibly far in the future")
        if (
            record.published_at
            and record.closing_at
            and record.closing_at < record.published_at - timedelta(days=1)
        ):
            result.add("closing_at", "closing date precedes the publication date")

        if record.estimated_value is not None and record.estimated_value < 0:
            result.add("estimated_value", "negative value")
        if record.estimated_value is not None and record.currency is None:
            result.add("currency", "value present without a currency")

        if record.submission_url and not is_http_url(record.submission_url):
            result.add("submission_url", "not a valid http(s) URL")
        for document in record.documents:
            if not is_http_url(document.source_url):
                result.add("documents", f"invalid document URL: {document.source_url[:120]}")
                break

        if record.procurement_type is ProcurementType.OTHER:
            result.add("procurement_type", "could not be determined from the source")
        if record.status is OpportunityStatus.UNKNOWN:
            result.add("status", "could not be determined from the source")
        if not record.reference_number:
            result.add("reference_number", "not published by the source")
        if not record.closing_at:
            result.add("closing_at", "not published or not parseable")

        result.completeness = self._completeness(record)
        result.quality = self._classify(record, result, hard_failure=hard_failure)
        return result

    def _completeness(self, record: NormalizedOpportunity) -> float:
        score = 0.0
        for name, weight in COMPLETENESS_WEIGHTS.items():
            value = getattr(record, name, None)
            if name == "documents":
                present = bool(record.documents)
            elif name == "contact":
                present = bool(record.contact)
            else:
                present = value not in (None, "", [], {})
            if present:
                score += weight
        return round(score, 3)

    def _classify(
        self, record: NormalizedOpportunity, result: ValidationResult, *, hard_failure: bool
    ) -> DataQuality:
        if hard_failure:
            return DataQuality.INVALID

        review_triggers = {
            "closing_at",
            "published_at",
            "estimated_value",
            "documents",
            "submission_url",
        }
        suspicious = {
            key
            for key, messages in result.issues.items()
            if key in review_triggers
            and any(
                word in " ".join(messages)
                for word in ("future", "precedes", "implausibly", "negative", "invalid")
            )
        }
        if suspicious:
            return DataQuality.NEEDS_REVIEW
        if result.completeness >= 0.7 and not {"title", "source_url"} & set(result.issues):
            return DataQuality.VALID
        if result.completeness >= 0.3:
            return DataQuality.INCOMPLETE
        return DataQuality.NEEDS_REVIEW
