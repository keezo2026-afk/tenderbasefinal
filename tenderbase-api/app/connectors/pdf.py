"""PDF repository connector.

Some municipalities publish nothing but a directory of PDF adverts. This
connector treats each linked PDF as a procurement item: it extracts the first
page(s) of text natively (never OCR by default) and derives a title, reference
number and dates with the shared normalization helpers.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.html import make_soup
from app.connectors.registry import register_connector
from app.enums import ConnectorType, DocumentFormat
from app.errors import ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.text import clean_text, normalize_reference_number
from app.utils.urls import filename_from_url, normalize_url

PDF_MAGIC = b"%PDF-"


@register_connector(default_for_type=True)
class PDFRepositoryConnector(ProcurementConnector):
    """Treats linked PDFs in a document library as procurement items."""

    key = "pdf.repository"
    name = "Generic PDF repository connector"
    connector_type = ConnectorType.PDF
    description = """
    Crawls a listing page for PDF links and creates one procurement item per
    PDF, using native text extraction (pypdf) on the first pages to derive a
    title and reference number. OCR is never invoked here.
    """
    config_schema = {
        "listing_paths": "list[str] — pages containing PDF links",
        "link_selector": "str — CSS selector for PDF anchors (default a[href$='.pdf'])",
        "max_documents": "int — safety ceiling per run (default 100)",
        "extract_pages": "int — pages of text to read per PDF (default 2)",
        "title_from": "'pdf' | 'link' — where the item title comes from (default link)",
    }

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        paths = source.get("listing_paths") or ["/"]
        return [
            DiscoveryTarget(url=normalize_url(path, base=source.base_url), kind="listing")
            for path in paths
        ]

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        if self.fetcher is None:  # pragma: no cover
            raise ParseError("No fetcher configured for connector")
        return await self.fetcher.fetch(target.url, source=source, target=target)

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        """Parse a listing page into one item per linked PDF."""
        if response.content.startswith(PDF_MAGIC):
            return [self._item_from_pdf(source, response, title=None)]

        selector = source.get("link_selector", "a[href$='.pdf']")
        soup = make_soup(response.text)
        max_documents = int(source.get("max_documents", 100))

        items: list[RawItem] = []
        seen: set[str] = set()
        for anchor in soup.select(selector)[:max_documents]:
            href = anchor.get("href")
            if not isinstance(href, str) or not href.strip():
                continue
            try:
                absolute = normalize_url(href, base=response.url)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            link_title = clean_text(anchor.get_text(" ", strip=True))
            filename = filename_from_url(absolute)
            items.append(
                RawItem(
                    source_url=absolute,
                    fields={
                        "title": link_title or filename or absolute,
                        "reference_number": normalize_reference_number(link_title or filename),
                    },
                    documents=[
                        DocumentCandidate(
                            source_url=absolute,
                            filename=filename,
                            title=link_title,
                            document_format=DocumentFormat.PDF,
                            mime_type="application/pdf",
                        )
                    ],
                    raw_payload={"listing_url": response.url, "link_text": link_title},
                    parser_metadata={
                        "connector": self.key,
                        "connector_version": self.version,
                        "listing_url": response.url,
                    },
                    observed_at=utcnow(),
                )
            )
        return items

    def _item_from_pdf(
        self, source: SourceContext, response: FetchResult, title: str | None
    ) -> RawItem:
        text = extract_pdf_preview(response.content, int(source.get("extract_pages", 2)))
        filename = filename_from_url(response.url)
        derived_title = title or first_meaningful_line(text) or filename or response.url
        return RawItem(
            source_url=response.url,
            fields={
                "title": derived_title,
                "description": text[:4000] if text else None,
                "reference_number": normalize_reference_number(filename),
            },
            documents=[
                DocumentCandidate(
                    source_url=response.url,
                    filename=filename,
                    document_format=DocumentFormat.PDF,
                    mime_type="application/pdf",
                )
            ],
            raw_payload={"bytes": len(response.content)},
            parser_metadata={
                "connector": self.key,
                "connector_version": self.version,
                "text_extracted": bool(text),
            },
            observed_at=utcnow(),
        )

    async def run(self, source: SourceContext):  # type: ignore[override]
        """Listing → PDF items, optionally enriching each item with PDF text."""
        title_from = source.get("title_from", "link")
        async for item in super().run(source):
            if title_from == "pdf":
                try:
                    pdf = await self.fetch(
                        source, DiscoveryTarget(url=item.source_url, kind="document", depth=1)
                    )
                except Exception as exc:  # noqa: BLE001 - degrade, never abort
                    item.parser_metadata["pdf_error"] = str(exc)
                    yield item
                    continue
                text = extract_pdf_preview(pdf.content, int(source.get("extract_pages", 2)))
                if text:
                    item.fields.setdefault("description", text[:4000])
                    if derived := first_meaningful_line(text):
                        item.fields["title"] = derived
                    item.parser_metadata["text_extracted"] = True
            yield item


def extract_pdf_preview(data: bytes, pages: int = 2) -> str:
    """Extract text from the first ``pages`` of a PDF (native, no OCR).

    Returns an empty string for scanned/image-only PDFs — the document engine
    decides whether OCR is warranted later.
    """
    if not data.startswith(PDF_MAGIC):
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        chunks: list[str] = []
        for page in reader.pages[: max(1, pages)]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - a broken page must not fail the doc
                continue
        return clean_text("\n".join(chunks)) or ""
    except Exception:  # noqa: BLE001 - encrypted/corrupt PDFs are common
        return ""


def first_meaningful_line(text: str, min_length: int = 12) -> str | None:
    """First reasonably long line of extracted text — a usable title heuristic."""
    for line in (text or "").splitlines():
        candidate = clean_text(line)
        if candidate and len(candidate) >= min_length:
            return candidate[:300]
    return None


def looks_like_pdf(data: bytes, headers: dict[str, Any] | None = None) -> bool:
    """Content-sniffing check that does not trust the filename."""
    if data.startswith(PDF_MAGIC):
        return True
    content_type = ((headers or {}).get("content-type") or "").lower()
    return "application/pdf" in content_type
