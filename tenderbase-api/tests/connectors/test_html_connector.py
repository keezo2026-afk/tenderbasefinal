"""HTML listing connector tests — driven entirely by saved fixtures."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.connectors.base import DiscoveryTarget, SourceContext
from app.connectors.html import HTMLListingConnector
from app.enums import ConnectorType

LISTING_CONFIG = {
    "listing_paths": ["/tenders"],
    "item_selector": "table.tenders tbody tr",
    "field_selectors": {
        "reference_number": "td:nth-child(1)",
        "title": "td:nth-child(2)",
        "published_at": "td:nth-child(3)",
        "closing_at": "td:nth-child(4)",
    },
    "link_selector": "td:nth-child(2) a",
    "document_selector": "td:nth-child(5) a",
}


def make_context(**config) -> SourceContext:
    return SourceContext(
        id=str(uuid4()),
        name="TEST FIXTURE municipality",
        organization="Test Fixture Municipality",
        base_url="https://example.org",
        connector_type=ConnectorType.HTML,
        config={**LISTING_CONFIG, **config},
    )


@pytest.fixture
def routes(fixture_loader):
    return {
        "https://example.org/tenders": (200, fixture_loader("html_listing.html"), "text/html"),
        "https://example.org/tenders/fixture-scm-2026-001": (
            200,
            fixture_loader("html_detail.html"),
            "text/html",
        ),
    }


async def test_discover_builds_absolute_targets(mock_fetcher, routes):
    connector = HTMLListingConnector(mock_fetcher(routes))
    targets = await connector.discover(make_context())
    assert [t.url for t in targets] == ["https://example.org/tenders"]


async def test_parses_every_row_of_the_listing(mock_fetcher, routes):
    fetcher = mock_fetcher(routes)
    connector = HTMLListingConnector(fetcher)
    context = make_context()
    response = await connector.fetch(context, DiscoveryTarget(url="https://example.org/tenders"))
    items = await connector.parse(context, response)

    assert len(items) == 3
    first = items[0]
    assert first.get("reference_number") == "FIXTURE/SCM/2026/001"
    assert "solar photovoltaic" in first.get("title")
    assert first.source_url == "https://example.org/tenders/fixture-scm-2026-001"
    assert first.parser_metadata["connector"] == "html.listing"
    await fetcher.aclose()


async def test_document_links_are_absolute_and_deduplicated(mock_fetcher, routes):
    fetcher = mock_fetcher(routes)
    connector = HTMLListingConnector(fetcher)
    context = make_context()
    response = await connector.fetch(context, DiscoveryTarget(url="https://example.org/tenders"))
    items = await connector.parse(context, response)

    second = items[1]
    urls = [doc.source_url for doc in second.documents]
    assert urls == [
        "https://example.org/documents/fixture-scm-2026-002.pdf",
        "https://example.org/documents/fixture-scm-2026-002-addendum.pdf",
    ]
    assert all(url.startswith("https://") for url in urls)
    await fetcher.aclose()


async def test_missing_selector_raises_a_configuration_error(mock_fetcher, routes):
    from app.errors import ParseError

    fetcher = mock_fetcher(routes)
    connector = HTMLListingConnector(fetcher)
    context = make_context()
    context.config.pop("item_selector")
    response = await connector.fetch(context, DiscoveryTarget(url="https://example.org/tenders"))
    with pytest.raises(ParseError):
        await connector.parse(context, response)
    await fetcher.aclose()


async def test_empty_listing_is_not_an_error(mock_fetcher):
    fetcher = mock_fetcher(
        {"https://example.org/tenders": (200, "<html><body>No tenders</body></html>", "text/html")}
    )
    connector = HTMLListingConnector(fetcher)
    context = make_context()
    response = await connector.fetch(context, DiscoveryTarget(url="https://example.org/tenders"))
    assert await connector.parse(context, response) == []
    await fetcher.aclose()


async def test_detail_following_enriches_items(mock_fetcher, routes):
    fetcher = mock_fetcher(routes)
    context = make_context(
        follow_detail=True,
        detail_field_selectors={
            "description": ".description",
            "briefing_location": ".briefing",
            "estimated_value": ".value",
            "submission_method": ".submission",
        },
    )
    connector = HTMLListingConnector(fetcher)

    items = [item async for item in connector.run(context)]
    enriched = items[0]
    assert "photovoltaic panels" in enriched.get("description")
    assert "Compulsory briefing" in enriched.get("briefing_location")
    assert enriched.get("estimated_value") == "R 1 250 000.00"
    # Detail-page documents are merged with the listing documents.
    assert any("specification" in doc.source_url for doc in enriched.documents)
    await fetcher.aclose()


async def test_detail_fetch_failure_degrades_gracefully(mock_fetcher, fixture_loader):
    fetcher = mock_fetcher(
        {"https://example.org/tenders": (200, fixture_loader("html_listing.html"), "text/html")}
    )
    context = make_context(follow_detail=True, detail_field_selectors={"description": ".d"})
    connector = HTMLListingConnector(fetcher)

    items = [item async for item in connector.run(context)]
    # Detail pages 404 in this route table, yet all listing rows still yield items.
    assert len(items) == 3
    assert any("detail_error" in item.parser_metadata for item in items)
    await fetcher.aclose()


async def test_pagination_is_bounded(mock_fetcher, fixture_loader):
    listing = fixture_loader("html_listing.html")
    fetcher = mock_fetcher(
        {
            "https://example.org/tenders": (200, listing, "text/html"),
            "https://example.org/tenders?page=2": (200, listing, "text/html"),
        }
    )
    context = make_context(pagination={"next_selector": "a.next", "max_pages": 2})
    connector = HTMLListingConnector(fetcher)
    items = [item async for item in connector.run(context)]
    assert len(items) == 6  # two pages of three rows, then the page limit stops it
    await fetcher.aclose()
