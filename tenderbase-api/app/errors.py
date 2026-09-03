"""Domain and API exception hierarchy.

Every error surfaced through the API carries a stable machine-readable code so
clients can branch on ``error.code`` rather than parsing messages.
"""

from __future__ import annotations

from typing import Any


class TenderBaseError(Exception):
    """Base class for all TenderBase errors."""

    code: str = "INTERNAL_ERROR"
    #: Whether running the same operation again could plausibly succeed.
    #: Ingestion uses this to choose between "back off and retry" and "record the
    #: failure, a human has to change something". Anything that is a fact about
    #: configuration (a missing connector, a refused URL, a malformed document)
    #: must stay False, or a worker will spend its life retrying a typo.
    retryable: bool = False
    http_status: int = 500
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.details = details or {}
        #: Extra response headers (e.g. ``Retry-After`` for 429). Never used to
        #: expose internals — only standard, client-actionable headers.
        self.headers = headers or {}
        super().__init__(self.message)


# --- API-facing errors ----------------------------------------------------


class NotFoundError(TenderBaseError):
    code = "NOT_FOUND"
    http_status = 404
    message = "Resource not found"


class TenderNotFoundError(NotFoundError):
    code = "TENDER_NOT_FOUND"
    message = "Tender not found"


class MunicipalityNotFoundError(NotFoundError):
    code = "MUNICIPALITY_NOT_FOUND"
    message = "Municipality not found"


class SourceNotFoundError(NotFoundError):
    code = "SOURCE_NOT_FOUND"
    message = "Source not found"


class DocumentNotFoundError(NotFoundError):
    code = "DOCUMENT_NOT_FOUND"
    message = "Document not found"


class ValidationError(TenderBaseError):
    code = "VALIDATION_ERROR"
    http_status = 422
    message = "Request validation failed"


class RateLimitedError(TenderBaseError):
    code = "RATE_LIMITED"
    http_status = 429
    message = "Too many requests"


class ServiceUnavailableError(TenderBaseError):
    code = "SERVICE_UNAVAILABLE"
    retryable = True
    http_status = 503
    message = "Service temporarily unavailable"


# --- Ingestion / connector errors ----------------------------------------


class IngestionError(TenderBaseError):
    code = "INGESTION_ERROR"
    message = "Ingestion failed"


class ConnectorError(IngestionError):
    code = "CONNECTOR_ERROR"
    message = "Connector failed"


class ConnectorNotRegisteredError(ConnectorError):
    code = "CONNECTOR_NOT_REGISTERED"
    message = "No connector registered for the requested type"


class FetchError(ConnectorError):
    code = "FETCH_ERROR"
    message = "Failed to fetch remote resource"


class RetryableFetchError(FetchError):
    retryable = True
    """Transient fetch failure — worth retrying with backoff."""

    code = "FETCH_RETRYABLE"


class PermanentFetchError(FetchError):
    """Non-transient fetch failure — retrying will not help."""

    code = "FETCH_PERMANENT"


class ParseError(ConnectorError):
    code = "PARSE_ERROR"
    message = "Failed to parse source response"


class NormalizationError(IngestionError):
    code = "NORMALIZATION_ERROR"
    message = "Failed to normalize record"


class UnsafeURLError(TenderBaseError):
    code = "UNSAFE_URL"
    http_status = 400
    message = "URL rejected by security policy"


class ResponseTooLargeError(FetchError):
    code = "RESPONSE_TOO_LARGE"
    message = "Remote response exceeded the configured size limit"


class RobotsDisallowedError(FetchError):
    code = "ROBOTS_DISALLOWED"
    message = "Fetching this URL is disallowed by the site's robots.txt"


class DocumentError(TenderBaseError):
    code = "DOCUMENT_ERROR"
    message = "Document processing failed"


class ExtractionError(DocumentError):
    code = "EXTRACTION_ERROR"
    message = "Text extraction failed"


class AIUnavailableError(TenderBaseError):
    code = "AI_UNAVAILABLE"
    http_status = 503
    message = "AI enrichment is not configured"
