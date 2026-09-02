"""Canonical enumerations shared by the database models, schemas and services.

All enums are *extensible*: values are stored as short strings in PostgreSQL
(``VARCHAR`` + CHECK-free native storage) rather than native PG enums, so a new
value never requires a blocking ``ALTER TYPE`` migration. Unknown source values
normalize to an explicit ``UNKNOWN``/``OTHER`` member instead of being dropped.
"""

from __future__ import annotations

from enum import StrEnum


class _Extensible(StrEnum):
    """StrEnum with a tolerant parser used by the normalization layer."""

    @classmethod
    def fallback(cls) -> _Extensible:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def parse(cls, value: object) -> _Extensible:
        """Parse a raw value, returning the fallback member when unrecognised."""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.fallback()
        candidate = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == candidate:
                return member
        return cls.fallback()


class MunicipalityType(_Extensible):
    METROPOLITAN = "METROPOLITAN"
    DISTRICT = "DISTRICT"
    LOCAL = "LOCAL"
    OTHER = "OTHER"

    @classmethod
    def fallback(cls) -> MunicipalityType:
        return cls.OTHER


class SourceType(_Extensible):
    NATIONAL_ETENDER = "NATIONAL_ETENDER"
    MUNICIPAL_WEBSITE = "MUNICIPAL_WEBSITE"
    MUNICIPAL_RFQ = "MUNICIPAL_RFQ"
    MUNICIPAL_TENDER = "MUNICIPAL_TENDER"
    PROVINCIAL_PORTAL = "PROVINCIAL_PORTAL"
    NATIONAL_DEPARTMENT = "NATIONAL_DEPARTMENT"
    STATE_OWNED_ENTITY = "STATE_OWNED_ENTITY"
    DOCUMENT_LIBRARY = "DOCUMENT_LIBRARY"
    WORDPRESS = "WORDPRESS"
    PDF_REPOSITORY = "PDF_REPOSITORY"
    CUSTOM = "CUSTOM"

    @classmethod
    def fallback(cls) -> SourceType:
        return cls.CUSTOM


class ConnectorType(_Extensible):
    HTTP = "HTTP"
    HTML = "HTML"
    WORDPRESS = "WORDPRESS"
    PDF = "PDF"
    BROWSER = "BROWSER"
    CUSTOM = "CUSTOM"

    @classmethod
    def fallback(cls) -> ConnectorType:
        return cls.CUSTOM


class ProcurementScope(_Extensible):
    NATIONAL = "NATIONAL"
    PROVINCIAL = "PROVINCIAL"
    DISTRICT = "DISTRICT"
    MUNICIPAL = "MUNICIPAL"
    ENTITY = "ENTITY"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> ProcurementScope:
        return cls.UNKNOWN


class HealthStatus(_Extensible):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> HealthStatus:
        return cls.UNKNOWN


class ProcurementType(_Extensible):
    TENDER = "TENDER"
    RFQ = "RFQ"
    RFB = "RFB"
    RFP = "RFP"
    RFI = "RFI"
    EOI = "EOI"
    EXPRESSION_OF_INTEREST = "EXPRESSION_OF_INTEREST"
    AUCTION = "AUCTION"
    OTHER = "OTHER"

    @classmethod
    def fallback(cls) -> ProcurementType:
        return cls.OTHER


class OpportunityStatus(_Extensible):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    AWARDED = "AWARDED"
    EXTENDED = "EXTENDED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> OpportunityStatus:
        return cls.UNKNOWN


class DataQuality(_Extensible):
    """Outcome of the validation engine for a normalized record."""

    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INVALID = "INVALID"

    @classmethod
    def fallback(cls) -> DataQuality:
        return cls.NEEDS_REVIEW


class EventType(_Extensible):
    OPPORTUNITY_CREATED = "OPPORTUNITY_CREATED"
    OPPORTUNITY_UPDATED = "OPPORTUNITY_UPDATED"
    DEADLINE_CHANGED = "DEADLINE_CHANGED"
    BRIEFING_CHANGED = "BRIEFING_CHANGED"
    DOCUMENT_ADDED = "DOCUMENT_ADDED"
    DOCUMENT_CHANGED = "DOCUMENT_CHANGED"
    DOCUMENT_REMOVED = "DOCUMENT_REMOVED"
    CONTACT_CHANGED = "CONTACT_CHANGED"
    SUBMISSION_CHANGED = "SUBMISSION_CHANGED"
    STATUS_CHANGED = "STATUS_CHANGED"
    CANCELLED = "CANCELLED"
    EXTENDED = "EXTENDED"
    AWARD_POSTED = "AWARD_POSTED"
    OTHER = "OTHER"

    @classmethod
    def fallback(cls) -> EventType:
        return cls.OTHER


class DocumentType(_Extensible):
    TENDER_DOCUMENT = "TENDER_DOCUMENT"
    RFQ_DOCUMENT = "RFQ_DOCUMENT"
    BID_FORM = "BID_FORM"
    SPECIFICATION = "SPECIFICATION"
    ADDENDUM = "ADDENDUM"
    BRIEFING_NOTES = "BRIEFING_NOTES"
    AWARD_NOTICE = "AWARD_NOTICE"
    ADVERT = "ADVERT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> DocumentType:
        return cls.UNKNOWN


class DocumentFormat(_Extensible):
    PDF = "PDF"
    DOC = "DOC"
    DOCX = "DOCX"
    XLS = "XLS"
    XLSX = "XLSX"
    CSV = "CSV"
    TXT = "TXT"
    HTML = "HTML"
    ZIP = "ZIP"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> DocumentFormat:
        return cls.UNKNOWN


class ExtractionMethod(_Extensible):
    NATIVE_PDF = "NATIVE_PDF"
    OCR = "OCR"
    HTML_PARSE = "HTML_PARSE"
    PLAIN_TEXT = "PLAIN_TEXT"
    SPREADSHEET = "SPREADSHEET"
    NONE = "NONE"

    @classmethod
    def fallback(cls) -> ExtractionMethod:
        return cls.NONE


class JobStatus(_Extensible):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"

    @classmethod
    def fallback(cls) -> JobStatus:
        return cls.QUEUED


class JobTrigger(_Extensible):
    SCHEDULER = "SCHEDULER"
    MANUAL = "MANUAL"
    API = "API"
    RETRY = "RETRY"

    @classmethod
    def fallback(cls) -> JobTrigger:
        return cls.MANUAL


class ErrorStage(_Extensible):
    DISCOVERY = "DISCOVERY"
    FETCH = "FETCH"
    PARSE = "PARSE"
    VALIDATE = "VALIDATE"
    NORMALIZE = "NORMALIZE"
    DEDUPLICATE = "DEDUPLICATE"
    VERSION = "VERSION"
    DOCUMENT = "DOCUMENT"
    PERSIST = "PERSIST"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def fallback(cls) -> ErrorStage:
        return cls.UNKNOWN


class DuplicateDecision(_Extensible):
    """Outcome of the deduplication engine."""

    NEW = "NEW"
    EXACT_MATCH = "EXACT_MATCH"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    UNCERTAIN = "UNCERTAIN"

    @classmethod
    def fallback(cls) -> DuplicateDecision:
        return cls.UNCERTAIN
