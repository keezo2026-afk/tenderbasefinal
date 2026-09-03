"""Document schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.enums import DocumentFormat, DocumentType, ExtractionMethod
from app.schemas.common import TenderBaseModel


class DocumentRead(TenderBaseModel):
    """Document metadata attached to an opportunity."""

    id: UUID
    opportunity_id: UUID
    source_url: str
    document_type: DocumentType = DocumentType.UNKNOWN
    document_format: DocumentFormat = DocumentFormat.UNKNOWN
    filename: str | None = None
    title: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    sha256: str | None = Field(default=None, description="SHA-256 of the downloaded bytes")
    page_count: int | None = None
    is_downloaded: bool = False
    published_at: datetime | None = None
    downloaded_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentVersionRead(TenderBaseModel):
    id: UUID
    document_id: UUID
    version: int
    sha256: str
    file_size: int | None = None
    mime_type: str | None = None
    downloaded_at: datetime


class DocumentTextRead(TenderBaseModel):
    id: UUID
    document_id: UUID
    extraction_method: ExtractionMethod = ExtractionMethod.NONE
    language: str | None = None
    char_count: int = 0
    page_count: int | None = None
    ocr_used: bool = False
    extraction_confidence: float | None = None
    extracted_at: datetime
    content: str | None = None


class DocumentCandidate(TenderBaseModel):
    """A document link discovered by a connector, before download."""

    source_url: str
    filename: str | None = None
    title: str | None = None
    document_type: DocumentType = DocumentType.UNKNOWN
    document_format: DocumentFormat = DocumentFormat.UNKNOWN
    mime_type: str | None = None
    published_at: datetime | None = None
