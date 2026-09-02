"""Text extraction.

    Document → validate → detect format → native extraction → OCR (only if
    needed) → cleaned text → classification

Native extraction is always attempted first; OCR is expensive and therefore
opt-in (``OCR_ENABLED``) and only triggered when native extraction yields too
little text for a document that plausibly contains scanned pages.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.enums import DocumentFormat, ExtractionMethod
from app.errors import ExtractionError
from app.logging import get_logger
from app.utils.text import clean_text

logger = get_logger("tenderbase.documents.extractor")

#: Below this many characters per page a PDF is considered image-only.
MIN_CHARS_PER_PAGE = 40
#: Cap on stored text per document (very large PDFs are truncated).
MAX_TEXT_CHARS = 2_000_000


@dataclass(slots=True)
class ExtractionResult:
    """Extracted text plus provenance about how it was produced."""

    text: str
    method: ExtractionMethod
    page_count: int | None = None
    char_count: int = 0
    ocr_used: bool = False
    confidence: float | None = None
    structure: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.char_count == 0


class TextExtractor:
    """Format-aware text extraction with an optional OCR fallback."""

    def __init__(self, *, settings: Settings | None = None, ocr: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._ocr = ocr

    def extract(
        self, data: bytes, *, document_format: DocumentFormat | str = DocumentFormat.UNKNOWN
    ) -> ExtractionResult:
        """Extract text from document bytes."""
        if not data:
            raise ExtractionError("Cannot extract text from empty content")

        fmt = DocumentFormat.parse(document_format)
        if fmt is DocumentFormat.UNKNOWN:
            fmt = self._sniff(data)

        handler = {
            DocumentFormat.PDF: self._extract_pdf,
            DocumentFormat.TXT: self._extract_text,
            DocumentFormat.CSV: self._extract_csv,
            DocumentFormat.HTML: self._extract_html,
            DocumentFormat.DOCX: self._extract_docx,
        }.get(fmt)

        if handler is None:
            logger.info("extractor.unsupported_format", format=str(fmt))
            return ExtractionResult(text="", method=ExtractionMethod.NONE)
        return handler(data)

    # -- format handlers --------------------------------------------------

    def _extract_pdf(self, data: bytes) -> ExtractionResult:
        pages: list[str] = []
        page_count = 0
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            page_count = len(reader.pages)
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - skip unreadable pages
                    pages.append("")
        except Exception as exc:  # noqa: BLE001 - encrypted/corrupt PDFs are common
            logger.warning("extractor.pdf_failed", error=str(exc))
            if not self._should_ocr(0, 1):
                raise ExtractionError(f"PDF could not be read: {exc}") from exc

        text = clean_text("\n\n".join(pages)) or ""
        density = len(text) / max(1, page_count)

        if self._should_ocr(len(text), page_count) and density < MIN_CHARS_PER_PAGE:
            ocr_result = self._run_ocr(data, page_count)
            if ocr_result is not None:
                return ocr_result

        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.NATIVE_PDF,
            page_count=page_count,
            char_count=min(len(text), MAX_TEXT_CHARS),
            confidence=1.0 if density >= MIN_CHARS_PER_PAGE else 0.4,
            structure={"page_lengths": [len(p) for p in pages][:1000]},
        )

    def _extract_text(self, data: bytes) -> ExtractionResult:
        text = clean_text(data.decode("utf-8", errors="replace")) or ""
        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.PLAIN_TEXT,
            char_count=min(len(text), MAX_TEXT_CHARS),
            confidence=1.0,
        )

    def _extract_csv(self, data: bytes) -> ExtractionResult:
        decoded = data.decode("utf-8", errors="replace")
        rows: list[str] = []
        try:
            for row in csv.reader(io.StringIO(decoded)):
                rows.append(" | ".join(cell.strip() for cell in row if cell))
        except csv.Error as exc:
            raise ExtractionError(f"CSV could not be parsed: {exc}") from exc
        text = clean_text("\n".join(rows)) or ""
        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.SPREADSHEET,
            char_count=min(len(text), MAX_TEXT_CHARS),
            confidence=1.0,
            structure={"row_count": len(rows)},
        )

    def _extract_html(self, data: bytes) -> ExtractionResult:
        from app.connectors.html import make_soup

        soup = make_soup(data.decode("utf-8", errors="replace"))
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = clean_text(soup.get_text("\n", strip=True)) or ""
        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.HTML_PARSE,
            char_count=min(len(text), MAX_TEXT_CHARS),
            confidence=0.9,
        )

    def _extract_docx(self, data: bytes) -> ExtractionResult:
        """DOCX text via the OOXML package (no extra dependency required)."""
        import re
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise ExtractionError(f"DOCX could not be read: {exc}") from exc

        xml = re.sub(r"</w:p>", "\n", xml)
        text = clean_text(re.sub(r"<[^>]+>", "", xml)) or ""
        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.PLAIN_TEXT,
            char_count=min(len(text), MAX_TEXT_CHARS),
            confidence=0.9,
        )

    # -- OCR --------------------------------------------------------------

    def _should_ocr(self, char_count: int, page_count: int) -> bool:
        if not self.settings.ocr_enabled:
            return False
        return char_count < MIN_CHARS_PER_PAGE * max(1, page_count)

    def _run_ocr(self, data: bytes, page_count: int) -> ExtractionResult | None:
        from app.documents.ocr import get_ocr_engine

        engine = self._ocr or get_ocr_engine(self.settings)
        if engine is None or not engine.available:
            logger.info("extractor.ocr_unavailable")
            return None
        try:
            ocr_text, confidence = engine.extract(data)
        except Exception as exc:  # noqa: BLE001 - OCR must never break ingestion
            logger.warning("extractor.ocr_failed", error=str(exc))
            return None
        text = clean_text(ocr_text) or ""
        return ExtractionResult(
            text=text[:MAX_TEXT_CHARS],
            method=ExtractionMethod.OCR,
            page_count=page_count or None,
            char_count=min(len(text), MAX_TEXT_CHARS),
            ocr_used=True,
            confidence=confidence,
        )

    def _sniff(self, data: bytes) -> DocumentFormat:
        from app.documents.downloader import sniff_format

        fmt, _ = sniff_format(data[:512])
        return fmt
