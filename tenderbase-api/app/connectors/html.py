"""Generic HTML listing connector.

Parses a listing page with configurable CSS selectors and (optionally) follows
detail pages. All selectors come from source configuration — nothing about a
specific municipality is hard-coded here, so one implementation serves many
sites.

Example ``source.config``::

    {
      "listing_paths": ["/tenders/"],
      "item_selector": "table.tenders tbody tr",
      "field_selectors": {
        "title": "td:nth-child(2)",
        "reference_number": "td:nth-child(1)",
        "published_at": "td:nth-child(3)",
        "closing_at": "td:nth-child(4)"
      },
      "link_selector": "a",
      "document_selector": "a[href$='.pdf']",
      "follow_detail": false,
      "pagination": {"next_selector": "a.next", "max_pages": 5}
    }
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.http import guess_format
from app.connectors.registry import register_connector
from app.enums import ConnectorType
from app.errors import ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.text import clean_text
from app.utils.urls import filename_from_url, is_http_url, normalize_url

DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".zip")

#: Sentinel distinguishing "no selector argument" from "selector is None".
_UNSET: Any = object()

#: Raw HTML above this size is not kept inline on the item (audit copies go to
#: the blob store via the pipeline's raw-payload handling).
MAX_INLINE_HTML = 200_000


def make_soup(markup: str) -> BeautifulSoup:
    """Parse HTML with lxml, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:  # noqa: BLE001 - lxml may be unavailable in slim images
        return BeautifulSoup(markup, "html.parser")


def select_text(node: Tag, selector: str | None) -> str | None:
    """Text of the first node matching ``selector`` (``None`` when absent)."""
    if not selector:
        return None
    found = node.select_one(selector)
    if found is None:
        return None
    return clean_text(found.get_text(" ", strip=True))


def select_attr(node: Tag, selector: str, attribute: str) -> str | None:
    found = node.select_one(selector)
    if found is None:
        return None
    value = found.get(attribute)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None


@register_connector(default_for_type=True)
class HTMLListingConnector(ProcurementConnector):
    """Selector-driven HTML listing connector."""

    key = "html.listing"
    name = "Generic HTML listing connector"
    connector_type = ConnectorType.HTML
    description = """
    Parses procurement listing pages (tables, card grids, article lists) using
    CSS selectors supplied in the source configuration. Supports optional
    pagination and optional detail-page following.
    """
    config_schema = {
        "listing_paths": "list[str] — listing page paths",
        "item_selector": "str — CSS selector matching one item per match",
        "field_selectors": "dict[canonical_field -> CSS selector]",
        "link_selector": "str — selector for the item's detail link",
        "document_selector": "str — selector for document links",
        "follow_detail": "bool — fetch each detail page (default false)",
        "detail_field_selectors": "dict — selectors applied to the detail page",
        "pagination": "{next_selector: str, max_pages: int}",
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
        item_selector = source.get("item_selector")
        if not item_selector:
            raise ParseError(
                "html.listing requires 'item_selector' in the source configuration",
                details={"source": source.name},
            )
        soup = make_soup(response.text)
        nodes = soup.select(item_selector)
        if not nodes:
            # An empty listing is legitimate (no current tenders); log via
            # parser metadata rather than raising.
            return []
        return [self._build_item(source, response, node) for node in nodes]

    def _build_item(self, source: SourceContext, response: FetchResult, node: Tag) -> RawItem:
        selectors: dict[str, str] = source.get("field_selectors") or {}
        fields: dict[str, Any] = {}
        for name, selector in selectors.items():
            if value := select_text(node, selector):
                fields[name] = value

        link_selector = source.get("link_selector", "a")
        href = select_attr(node, link_selector, "href") if link_selector else None
        detail_url = response.url
        if href:
            try:
                detail_url = normalize_url(href, base=response.url)
            except Exception:  # noqa: BLE001
                detail_url = response.url
        fields.setdefault("title", select_text(node, link_selector) or "")
        if not fields.get("title"):
            fields["title"] = clean_text(node.get_text(" ", strip=True)) or ""

        raw_html = str(node)
        return RawItem(
            source_url=detail_url,
            fields={k: v for k, v in fields.items() if v},
            documents=list(self.collect_documents(source, node, response.url)),
            raw_payload={"listing_url": response.url},
            raw_html=raw_html if len(raw_html) <= MAX_INLINE_HTML else None,
            parser_metadata={
                "connector": self.key,
                "connector_version": self.version,
                "item_selector": source.get("item_selector"),
                "listing_url": response.url,
            },
            observed_at=utcnow(),
        )

    def collect_documents(
        self,
        source: SourceContext,
        node: Tag,
        base_url: str,
        *,
        selector: str | None = _UNSET,
    ) -> Iterable[DocumentCandidate]:
        """Collect document links inside an item (or detail page) node."""
        if selector is _UNSET:
            selector = source.get("document_selector")
        anchors = node.select(selector) if selector else node.select("a[href]")
        seen: set[str] = set()
        for anchor in anchors:
            href = anchor.get("href")
            if not isinstance(href, str) or not href.strip():
                continue
            lowered = href.lower().split("?")[0]
            if not selector and not lowered.endswith(DOCUMENT_EXTENSIONS):
                continue
            try:
                absolute = normalize_url(href, base=base_url)
            except Exception:  # noqa: BLE001
                continue
            if not is_http_url(absolute) or absolute in seen:
                continue
            seen.add(absolute)
            filename = filename_from_url(absolute)
            yield DocumentCandidate(
                source_url=absolute,
                filename=filename,
                title=clean_text(anchor.get_text(" ", strip=True)),
                document_format=guess_format(filename),
            )

    async def run(self, source: SourceContext):  # type: ignore[override]
        """Driver with optional pagination and detail-page following."""
        pagination = source.get("pagination") or {}
        next_selector = pagination.get("next_selector")
        max_pages = int(pagination.get("max_pages", 1))
        follow_detail = bool(source.get("follow_detail"))
        detail_selectors: dict[str, str] = source.get("detail_field_selectors") or {}

        for target in await self.discover(source):
            page_url: str | None = target.url
            pages = 0
            while page_url and pages < max_pages:
                response = await self.fetch(source, DiscoveryTarget(url=page_url, kind="listing"))
                pages += 1
                for item in await self.parse(source, response):
                    if not await self.validate(source, item):
                        continue
                    if follow_detail and item.source_url != response.url:
                        item = await self._enrich_from_detail(source, item, detail_selectors)
                    item.documents = list(await self.extract_documents(source, item))
                    yield item

                page_url = None
                if next_selector:
                    soup = make_soup(response.text)
                    if href := select_attr(soup, next_selector, "href"):
                        try:
                            candidate = normalize_url(href, base=response.url)
                        except Exception:  # noqa: BLE001
                            candidate = None
                        if candidate and candidate != response.url:
                            page_url = candidate

    async def _enrich_from_detail(
        self, source: SourceContext, item: RawItem, selectors: dict[str, str]
    ) -> RawItem:
        """Fetch the detail page and merge additional fields/documents."""
        try:
            detail = await self.fetch(
                source, DiscoveryTarget(url=item.source_url, kind="detail", depth=1)
            )
        except Exception as exc:  # noqa: BLE001 - detail failures degrade, not abort
            item.parser_metadata["detail_error"] = str(exc)
            return item

        soup = make_soup(detail.text)
        body = soup.body or soup
        for name, selector in selectors.items():
            if value := select_text(body, selector):
                item.fields.setdefault(name, value)
        if not item.fields.get("description"):
            if description_selector := source.get("description_selector"):
                if value := select_text(body, description_selector):
                    item.fields["description"] = value

        existing = {doc.source_url for doc in item.documents}
        # Detail pages rarely share the listing's document selector, so fall
        # back to extension-based link detection unless one is configured.
        detail_selector = source.get("detail_document_selector")
        for candidate in self.collect_documents(source, body, detail.url, selector=detail_selector):
            if candidate.source_url not in existing:
                item.documents.append(candidate)
                existing.add(candidate.source_url)
        item.parser_metadata["detail_fetched"] = True
        return item
