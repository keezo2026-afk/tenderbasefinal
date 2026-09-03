"""Browser connector (Playwright).

Some procurement portals render their listings client-side. This connector
renders the page in a headless browser and hands the resulting DOM to the
generic HTML parsing logic.

Playwright is an **optional** dependency: it is imported lazily so the API and
the rest of the ingestion system run in a slim image without it. When it is not
installed the connector raises a clear, actionable error instead of failing
obscurely.

It is used only for legitimately public pages: it does not solve CAPTCHAs, log
in, or evade bot protection.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.html import HTMLListingConnector
from app.connectors.registry import register_connector
from app.enums import ConnectorType
from app.errors import ConnectorError, UnsafeURLError
from app.logging import get_logger
from app.utils.urls import normalize_url, validate_url

logger = get_logger("tenderbase.connectors.browser")

DEFAULT_WAIT_MS = 5_000
MAX_WAIT_MS = 30_000


class BrowserRenderer:
    """Thin async wrapper around Playwright Chromium.

    Injected into the connector so tests can substitute a stub renderer and
    never require a real browser.
    """

    def __init__(self, *, user_agent: str, headless: bool = True) -> None:
        self.user_agent = user_agent
        self.headless = headless

    async def render(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        wait_ms: int = DEFAULT_WAIT_MS,
        timeout_ms: int = MAX_WAIT_MS,
    ) -> tuple[str, int]:
        """Return ``(html, status_code)`` for a rendered page."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConnectorError(
                "Playwright is not installed. Install the 'browser' extra "
                "(pip install -e '.[browser]' && playwright install chromium) "
                "to use BROWSER connectors.",
                code="BROWSER_UNAVAILABLE",
            ) from exc

        async with async_playwright() as playwright:  # pragma: no cover - needs a browser
            browser = await playwright.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(user_agent=self.user_agent)
                page = await context.new_page()
                response = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_for_selector:
                    await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
                else:
                    await page.wait_for_timeout(min(wait_ms, timeout_ms))
                html = await page.content()
                status = response.status if response else 200
                return html, status
            finally:
                await browser.close()


@register_connector(default_for_type=True)
class BrowserConnector(ProcurementConnector):
    """Renders JavaScript-driven listings, then parses them as HTML."""

    key = "browser.playwright"
    name = "Headless browser connector"
    connector_type = ConnectorType.BROWSER
    requires_browser = True
    #: Requires the optional ``browser`` extra plus downloaded Chromium; not
    #: part of the base image, and not exercised in CI.
    production_ready = False
    status_note = (
        "Optional connector: requires `pip install '.[browser']` and "
        "`playwright install chromium`. Never enabled unless a source genuinely "
        "needs rendered HTML."
    )
    description = """
    Renders JavaScript-driven procurement listings with headless Chromium and
    parses the resulting DOM using the generic HTML selector logic. Requires
    the optional 'browser' extra. Used only for publicly accessible pages —
    it performs no authentication or anti-bot evasion.
    """
    config_schema = {
        "listing_paths": "list[str] — listing page paths",
        "item_selector": "str — CSS selector matching one item per match",
        "field_selectors": "dict[canonical_field -> CSS selector]",
        "wait_for_selector": "str — selector to await before capturing the DOM",
        "wait_ms": "int — fixed wait when no selector is given (default 5000)",
    }

    def __init__(self, fetcher: Any | None = None, renderer: BrowserRenderer | None = None) -> None:
        super().__init__(fetcher)
        self.renderer = renderer
        self._html = HTMLListingConnector(fetcher)

    def _get_renderer(self) -> BrowserRenderer:
        if self.renderer is None:
            from app.config import get_settings

            self.renderer = BrowserRenderer(user_agent=get_settings().http_user_agent)
        return self.renderer

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        paths = source.get("listing_paths") or ["/"]
        return [
            DiscoveryTarget(url=normalize_url(path, base=source.base_url), kind="listing")
            for path in paths
        ]

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        from app.config import get_settings

        settings = get_settings()
        check = validate_url(
            target.url, allow_private_networks=settings.http_allow_private_networks
        )
        if not check.ok:
            raise UnsafeURLError(f"Rejected URL: {check.reason}", details={"url": target.url})

        html, status = await self._get_renderer().render(
            check.url,
            wait_for_selector=source.get("wait_for_selector"),
            wait_ms=min(int(source.get("wait_ms", DEFAULT_WAIT_MS)), MAX_WAIT_MS),
        )
        payload = html.encode("utf-8")
        if len(payload) > settings.http_max_response_bytes:
            payload = payload[: settings.http_max_response_bytes]
            logger.warning("browser.render_truncated", url=check.url)
        return FetchResult(
            target=target,
            url=check.url,
            status_code=status,
            content=payload,
            headers={"content-type": "text/html; charset=utf-8"},
            encoding="utf-8",
        )

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        items = await self._html.parse(source, response)
        for item in items:
            item.parser_metadata["connector"] = self.key
            item.parser_metadata["rendered"] = True
        return items
