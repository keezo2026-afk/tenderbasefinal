"""Document service: metadata reads plus the download/extract/classify workflow."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import Document, DocumentText, DocumentVersion
from app.documents.classifier import DocumentClassifier
from app.documents.downloader import DocumentDownloader
from app.documents.extractor import TextExtractor
from app.enums import DocumentType
from app.errors import DocumentNotFoundError
from app.logging import get_logger
from app.schemas.common import PaginationParams
from app.utils.dates import utcnow

logger = get_logger("tenderbase.services.document")


class DocumentService:
    """Reads document metadata and orchestrates document processing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- reads ------------------------------------------------------------

    async def get_document(self, document_id: UUID) -> Document:
        stmt = select(Document).where(Document.id == document_id)
        document = (await self.session.execute(stmt)).scalars().first()
        if document is None:
            raise DocumentNotFoundError(details={"id": str(document_id)})
        return document

    async def list_versions(self, document_id: UUID) -> list[DocumentVersion]:
        await self.get_document(document_id)
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_text(self, document_id: UUID) -> DocumentText | None:
        await self.get_document(document_id)
        stmt = select(DocumentText).where(DocumentText.document_id == document_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_documents(
        self, pagination: PaginationParams, *, opportunity_id: UUID | None = None
    ) -> tuple[list[Document], int]:
        stmt = select(Document)
        if opportunity_id:
            stmt = stmt.where(Document.opportunity_id == opportunity_id)
        total = await self._count(stmt)
        stmt = (
            stmt.order_by(Document.created_at.desc(), Document.id.asc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    async def pending_downloads(self, limit: int = 50) -> list[Document]:
        """Registered documents whose bytes have not been fetched yet."""
        stmt = (
            select(Document)
            .where(Document.is_downloaded.is_(False), Document.download_error.is_(None))
            .order_by(Document.created_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -- processing -------------------------------------------------------

    async def process_document(
        self,
        document: Document,
        *,
        downloader: DocumentDownloader,
        extractor: TextExtractor | None = None,
        classifier: DocumentClassifier | None = None,
        extract_text: bool = True,
    ) -> Document:
        """Download → hash → store → extract text → classify.

        Every failure is recorded on the document rather than raised, so a
        single unreachable file never stops a batch.
        """
        result = await downloader.download_document(self.session, document)
        if result is None:
            return document

        if extract_text and result.changed:
            await self._extract_text(document, downloader, extractor or TextExtractor())

        text_row = await self.get_text(document.id) if extract_text else None
        classification = (classifier or DocumentClassifier()).classify(
            filename=document.filename,
            title=document.title,
            text=text_row.content if text_row else None,
        )
        if classification.document_type is not DocumentType.UNKNOWN:
            document.document_type = str(classification.document_type)
        await self.session.flush()
        return document

    async def _extract_text(
        self, document: Document, downloader: DocumentDownloader, extractor: TextExtractor
    ) -> None:
        if not document.storage_key:
            return
        try:
            data = downloader.storage.read_bytes(document.storage_key)
            extraction = extractor.extract(data, document_format=document.document_format)
        except Exception as exc:  # noqa: BLE001 - extraction failures are data
            logger.warning(
                "document.extraction_failed", document_id=str(document.id), error=str(exc)
            )
            return

        existing = await self.get_text(document.id)
        if existing is None:
            existing = DocumentText(document_id=document.id, extracted_at=utcnow())
            self.session.add(existing)
        existing.content = extraction.text or None
        existing.char_count = extraction.char_count
        existing.page_count = extraction.page_count
        existing.extraction_method = str(extraction.method)
        existing.ocr_used = extraction.ocr_used
        existing.extraction_confidence = extraction.confidence
        existing.structure = extraction.structure or None
        existing.extracted_at = utcnow()
        if extraction.page_count:
            document.page_count = extraction.page_count

    async def _count(self, stmt: Select[Any]) -> int:
        subquery = stmt.order_by(None).subquery()
        return int(
            (await self.session.execute(select(func.count()).select_from(subquery))).scalar_one()
        )
