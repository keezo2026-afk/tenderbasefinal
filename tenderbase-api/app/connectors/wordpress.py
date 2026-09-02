"""WordPress connector.

A large share of South African municipal websites run WordPress. Where the
standard REST API (``/wp-json/wp/v2/...``) is publicly exposed it is far more
reliable than scraping themed HTML, so this connector prefers it and falls back
to the generic HTML listing connector when the API is unavailable.

Example ``source.config``::

    {
      "post_type": "posts",              # or "pages", or a custom post type
      "search": "tender",
      "categories": [12],
      "per_page": 50,
      "max_pages": 3,
      "html_fallback": {"item_selector": "article", ...}
    }
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.html import HTMLListingConnector, make_soup
from app.connectors.http import guess_format
from app.connectors.registry import register_connector
from app.enums import ConnectorType
from app.errors import FetchError, ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.text import clean_text
from app.utils.urls import filename_from_url, normalize_url, with_query

WP_API_PATH = "/wp-json/wp/v2"
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip")


@register_connector(default_for_type=True)
class WordPressConnector(ProcurementConnector):
    """Reads procurement posts from the WordPress REST API."""

    key = "wordpress.rest"
    name = "WordPress REST connector"
    connector_type = ConnectorType.WORDPRESS
    description = """
    Uses the public WordPress REST API (/wp-json/wp/v2) to list procurement
    posts or a custom post type, then extracts document links from the rendered
    content. Falls back to HTML listing parsing when the REST API is not
    exposed by the site.
    """
    config_schema = {
        "post_type": "str — REST collection (posts, pages, or a custom type)",
        "search": "str — search term applied server-side",
        "categories": "list[int] — WordPress category IDs",
        "per_page": "int — page size (max 100)",
        "max_pages": "int — pagination limit",
        "html_fallback": "dict — html.listing config used if the API is absent",
    }

    def __init__(self, fetcher: Any | None = None) -> None:
        super().__init__(fetcher)
        self._html = HTMLListingConnector(fetcher)

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        post_type = source.get("post_type", "posts")
        per_page = min(int(source.get("per_page", 50)), 100)
        max_pages = max(1, int(source.get("max_pages", 1)))
        base = normalize_url(f"{WP_API_PATH}/{post_type}", base=source.base_url)

        targets: list[DiscoveryTarget] = []
        for page in range(1, max_pages + 1):
            url = with_query(base, per_page=per_page, page=page, _embed=1)
            if search := source.get("search"):
                url = with_query(url, search=search)
            if categories := source.get("categories"):
                url = with_query(url, categories=",".join(str(c) for c in categories))
            targets.append(DiscoveryTarget(url=url, kind="listing", metadata={"page": page}))
        return targets

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        if self.fetcher is None:  # pragma: no cover
            raise ParseError("No fetcher configured for connector")
        return await self.fetcher.fetch(
            target.url, source=source, target=target, headers={"Accept": "application/json"}
        )

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(
                "WordPress REST response was not JSON — the API may be disabled",
                details={"url": response.url},
            ) from exc
        if isinstance(payload, dict) and payload.get("code"):
            raise ParseError(
                f"WordPress REST error: {payload.get('code')}", details={"url": response.url}
            )
        if not isinstance(payload, list):
            raise ParseError("Unexpected WordPress REST payload", details={"url": response.url})
        return [
            self._build_item(source, response, post) for post in payload if isinstance(post, dict)
        ]

    def _build_item(
        self, source: SourceContext, response: FetchResult, post: dict[str, Any]
    ) -> RawItem:
        title = clean_text(_rendered(post.get("title"))) or ""
        content_html = _rendered(post.get("content")) or ""
        excerpt = clean_text(_strip_html(_rendered(post.get("excerpt")) or ""))
        link = post.get("link") or response.url

        fields: dict[str, Any] = {
            "title": title,
            "description": excerpt or clean_text(_strip_html(content_html)),
            "published_at": post.get("date_gmt") or post.get("date"),
            "external_id": str(post.get("id")) if post.get("id") is not None else None,
        }
        if modified := post.get("modified_gmt"):
            fields["modified_at"] = modified

        documents = self._documents_from_html(content_html, str(link), source)
        for media in _embedded_media(post):
            try:
                absolute = normalize_url(media, base=str(link))
            except Exception:  # noqa: BLE001
                continue
            if absolute not in {doc.source_url for doc in documents}:
                filename = filename_from_url(absolute)
                documents.append(
                    DocumentCandidate(
                        source_url=absolute,
                        filename=filename,
                        document_format=guess_format(filename),
                    )
                )

        return RawItem(
            source_url=normalize_url(str(link), base=source.base_url),
            fields={k: v for k, v in fields.items() if v},
            documents=documents,
            raw_payload={
                "id": post.get("id"),
                "slug": post.get("slug"),
                "type": post.get("type"),
                "status": post.get("status"),
                "link": post.get("link"),
                "date_gmt": post.get("date_gmt"),
                "modified_gmt": post.get("modified_gmt"),
            },
            parser_metadata={
                "connector": self.key,
                "connector_version": self.version,
                "listing_url": response.url,
            },
            observed_at=utcnow(),
        )

    def _documents_from_html(
        self, html: str, base_url: str, source: SourceContext
    ) -> list[DocumentCandidate]:
        if not html:
            return []
        soup = make_soup(html)
        candidates: list[DocumentCandidate] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            if not href.lower().split("?")[0].endswith(DOCUMENT_EXTENSIONS):
                continue
            try:
                absolute = normalize_url(href, base=base_url)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            filename = filename_from_url(absolute)
            candidates.append(
                DocumentCandidate(
                    source_url=absolute,
                    filename=filename,
                    title=clean_text(anchor.get_text(" ", strip=True)),
                    document_format=guess_format(filename),
                )
            )
        return candidates

    async def run(self, source: SourceContext):  # type: ignore[override]
        """Try the REST API; fall back to HTML listing parsing when absent."""
        fallback = source.get("html_fallback")
        try:
            produced = False
            async for item in super().run(source):
                produced = True
                yield item
            if produced or not fallback:
                return
        except (ParseError, FetchError):
            # A disabled REST API surfaces as a 404/permanent fetch error or a
            # non-JSON body; both are legitimate reasons to fall back to HTML.
            if not fallback:
                raise

        fallback_source = SourceContext(
            id=source.id,
            name=source.name,
            organization=source.organization,
            base_url=source.base_url,
            connector_type=ConnectorType.HTML,
            config={**fallback, "listing_paths": fallback.get("listing_paths", ["/"])},
            source_type=source.source_type,
            municipality_id=source.municipality_id,
            province_id=source.province_id,
            rate_limit_per_minute=source.rate_limit_per_minute,
            robots_policy=source.robots_policy,
            source_timezone=source.source_timezone,
        )
        async for item in self._html.run(fallback_source):
            item.parser_metadata["fallback"] = "html.listing"
            yield item


def _rendered(value: Any) -> str | None:
    """WordPress renders fields as ``{"rendered": "..."}``."""
    if isinstance(value, dict):
        rendered = value.get("rendered")
        return str(rendered) if rendered is not None else None
    return str(value) if value is not None else None


def _strip_html(html: str) -> str:
    return make_soup(html).get_text(" ", strip=True) if html else ""


def _embedded_media(post: dict[str, Any]) -> list[str]:
    """Extract attachment URLs from an ``_embed``-ed WordPress payload."""
    embedded = post.get("_embedded") or {}
    urls: list[str] = []
    for group in embedded.values():
        if not isinstance(group, list):
            continue
        for entry in group:
            entries = entry if isinstance(entry, list) else [entry]
            for media in entries:
                if isinstance(media, dict) and isinstance(media.get("source_url"), str):
                    if media["source_url"].lower().split("?")[0].endswith(DOCUMENT_EXTENSIONS):
                        urls.append(media["source_url"])
    return urls
