"""Document downloader.

Safety rules enforced here:

* only validated http(s) URLs (SSRF guard, redirect re-validation)
* hard maximum download size, enforced while streaming
* content-type/magic-byte sniffing — filenames are never trusted
* SHA-256 of the bytes is the document's identity
* files are stored content-addressed; nothing is ever executed
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.document import Document, DocumentVersion
from app.documents.storage import BlobStorage, build_storage_key, get_document_storage
from app.enums import DocumentFormat
from app.errors import DocumentError
from app.logging import get_logger
from app.utils.dates import utcnow
from app.utils.hashing import sha256_stream
from app.utils.urls import filename_from_url

logger = get_logger("tenderbase.documents.downloader")

#: Magic-byte signatures used to detect the real format of a download.
MAGIC_SIGNATURES: tuple[tuple[bytes, DocumentFormat, str], ...] = (
    (b"%PDF-", DocumentFormat.PDF, "application/pdf"),
    (b"PK\x03\x04", DocumentFormat.ZIP, "application/zip"),  # also docx/xlsx
    (b"\xd0\xcf\x11\xe0", DocumentFormat.DOC, "application/msword"),  # OLE2: doc/xls
    (b"<!DOCTYPE html", DocumentFormat.HTML, "text/html"),
    (b"<html", DocumentFormat.HTML, "text/html"),
)

#: Content types we are willing to persist as documents.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "application/octet-stream",
        "text/plain",
        "text/csv",
        "text/html",
    }
)

OOXML_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocumentFormat.DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentFormat.XLSX,
}


@dataclass(slots=True)
class DownloadResult:
    """Result of downloading (or skipping) one document."""

    sha256: str
    size: int
    mime_type: str | None
    document_format: DocumentFormat
    storage_key: str
    downloaded_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    changed: bool = True


class DocumentDownloader:
    """Downloads documents safely and records their versions."""

    def __init__(
        self,
        *,
        fetcher: Any,
        storage: BlobStorage | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetcher = fetcher
        self.storage = storage or get_document_storage(self.settings)

    async def download(self, url: str, *, max_bytes: int | None = None) -> DownloadResult:
        """Stream a remote document to storage and return its metadata."""
        limit = max_bytes or self.settings.document_max_bytes
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            temporary = Path(handle.name)
            try:
                metadata = await self.fetcher.stream_to(url, sink=handle, max_bytes=limit)
            except Exception:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise

        try:
            size = temporary.stat().st_size
            if size == 0:
                raise DocumentError("Downloaded document is empty", details={"url": url})

            with temporary.open("rb") as stream:
                digest = sha256_stream(stream)
            with temporary.open("rb") as stream:
                head = stream.read(512)

            declared_type = (metadata.get("content_type") or "").lower() or None
            document_format, sniffed_type = sniff_format(
                head, declared_type=declared_type, filename=filename_from_url(url)
            )
            mime_type = sniffed_type or declared_type
            if mime_type and mime_type not in ALLOWED_CONTENT_TYPES:
                raise DocumentError(
                    f"Refusing to store unsupported content type '{mime_type}'",
                    details={"url": url},
                )

            key = build_storage_key(
                namespace="documents",
                digest=digest,
                extension=str(document_format).lower() if document_format else None,
            )
            if not self.storage.exists(key):
                with temporary.open("rb") as source, self.storage.open_write(key) as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)

            logger.info(
                "document.downloaded",
                url=url,
                bytes=size,
                sha256=digest[:16],
                format=str(document_format),
            )
            return DownloadResult(
                sha256=digest,
                size=size,
                mime_type=mime_type,
                document_format=document_format,
                storage_key=key,
                downloaded_at=utcnow(),
                etag=metadata.get("etag"),
                last_modified=metadata.get("last_modified"),
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def download_document(
        self, session: AsyncSession, document: Document
    ) -> DownloadResult | None:
        """Download a registered document and persist a version when it changed."""
        try:
            result = await self.download(document.source_url)
        except Exception as exc:  # noqa: BLE001 - failures are recorded, not raised
            document.download_error = str(exc)[:2000]
            logger.warning("document.download_failed", url=document.source_url, error=str(exc))
            return None

        existing = (
            (
                await session.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.sha256 == result.sha256,
                    )
                )
            )
            .scalars()
            .first()
        )
        result.changed = existing is None

        if existing is None:
            latest = (
                (
                    await session.execute(
                        select(DocumentVersion.version)
                        .where(DocumentVersion.document_id == document.id)
                        .order_by(DocumentVersion.version.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            next_version = (latest or 0) + 1
            session.add(
                DocumentVersion(
                    document_id=document.id,
                    version=next_version,
                    sha256=result.sha256,
                    file_size=result.size,
                    mime_type=result.mime_type,
                    storage_key=result.storage_key,
                    etag=result.etag,
                    downloaded_at=result.downloaded_at,
                )
            )
            document.current_version = next_version

        document.sha256 = result.sha256
        document.file_size = result.size
        document.mime_type = result.mime_type
        document.document_format = str(result.document_format)
        document.storage_key = result.storage_key
        document.downloaded_at = result.downloaded_at
        document.is_downloaded = True
        document.download_error = None
        if not document.filename:
            document.filename = filename_from_url(document.source_url)
        return result


def sniff_format(
    head: bytes, *, declared_type: str | None = None, filename: str | None = None
) -> tuple[DocumentFormat, str | None]:
    """Determine a document's real format from its magic bytes.

    The declared content type and the filename are only *hints*; the bytes win.
    """
    for signature, fmt, mime in MAGIC_SIGNATURES:
        if head.startswith(signature) or (
            fmt is DocumentFormat.HTML and signature.lower() in head[:200].lower()
        ):
            if fmt is DocumentFormat.ZIP and declared_type in OOXML_TYPES:
                return OOXML_TYPES[declared_type], declared_type
            if fmt is DocumentFormat.ZIP and filename:
                extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if extension in {"docx", "xlsx", "pptx"}:
                    return DocumentFormat.parse(extension), declared_type
            return fmt, mime

    if declared_type in OOXML_TYPES:
        return OOXML_TYPES[declared_type], declared_type
    if declared_type == "text/csv":
        return DocumentFormat.CSV, declared_type
    if declared_type == "text/plain":
        return DocumentFormat.TXT, declared_type
    if filename and "." in filename:
        return DocumentFormat.parse(filename.rsplit(".", 1)[-1]), declared_type
    return DocumentFormat.UNKNOWN, declared_type
