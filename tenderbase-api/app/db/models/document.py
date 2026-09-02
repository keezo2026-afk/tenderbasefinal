"""Documents, document versions and extracted document text."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import DocumentFormat, DocumentType, ExtractionMethod


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A procurement document attached to an opportunity.

    Filenames are never treated as identity — ``sha256`` of the bytes is.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "source_url", name="uq_documents_opportunity_url"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_opportunity_id_created", "opportunity_id", "created_at"),
        CheckConstraint("file_size IS NULL OR file_size >= 0", name="file_size_non_negative"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("procurement_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentType.UNKNOWN
    )
    document_format: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentFormat.UNKNOWN
    )
    filename: Mapped[str | None] = mapped_column(String(400), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: False until the file has been fetched, hashed and stored.
    is_downloaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    opportunity: Mapped[ProcurementOpportunity] = relationship(  # noqa: F821
        back_populates="documents"
    )
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="noload"
    )
    text: Mapped[DocumentText | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="noload",
    )


class DocumentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A distinct byte-level revision of a document at the same URL."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "sha256", name="uq_document_versions_document_sha256"),
        Index("ix_document_versions_document_id_version", "document_id", "version"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_modified: Mapped[datetime | None] = mapped_column(nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(nullable=False)

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentText(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Extracted, cleaned text for a document version."""

    __tablename__ = "document_text"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_text_document_id"),
        Index("ix_document_text_extraction_method", "extraction_method"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True
    )
    extraction_method: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExtractionMethod.NONE
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Per-page text offsets etc., for future citation/highlighting features.
    structure: Mapped[dict | None] = mapped_column(nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(nullable=False)

    document: Mapped[Document] = relationship(back_populates="text")
