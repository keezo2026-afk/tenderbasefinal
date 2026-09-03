"""Tests for the HTTP/JSON, WordPress, PDF and eTender (OCDS) connectors."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.connectors.base import DiscoveryTarget, SourceContext
from app.connectors.custom.etender import ETenderOCDSConnector
from app.connectors.http import HTTPJSONConnector, dig
from app.connectors.pdf import PDFRepositoryConnector, first_meaningful_line
from app.connectors.wordpress import WordPressConnector
from app.enums import ConnectorType, DocumentFormat, ProcurementType
from app.errors import ParseError


def context(connector_type: ConnectorType, **config) -> SourceContext:
    return SourceContext(
        id=str(uuid4()),
        name="TEST FIXTURE source",
        organization="Test Fixture Organisation",
        base_url="https://example.org",
        connector_type=connector_type,
        config=config,
    )


# --- HTTP / JSON ----------------------------------------------------------


def test_dig_resolves_dotted_paths():
    payload = {"a": {"b": [{"c": 1}]}}
    assert dig(payload, "a.b.0.c") == 1
    assert dig(payload, "a.missing", default="x") == "x"
    assert dig(payload, None, default="x") == "x"


async def test_http_json_connector_maps_fields(mock_fetcher, fixture_loader):
    fetcher = mock_fetcher(
        {
            "https://example.org/api/tenders": (
                200,
                fixture_loader("http_json_listing.json"),
                "application/json",
            )
        }
    )
    source = context(
        ConnectorType.HTTP,
        listing_paths=["/api/tenders"],
        records_path="data.items",
        field_map={
            "title": "title",
            "reference_number": "referenceNumber",
            "published_at": "datePublished",
            "closing_at": "closingDate",
            "description": "summary",
            "detail_url": "links.self",
        },
        document_path="attachments",
        document_url_key="url",
    )
    connector = HTTPJSONConnector(fetcher)
    items = [item async for item in connector.run(source)]

    assert len(items) == 2
    assert items[0].get("reference_number") == "FIXTURE/JSON/2026/001"
    assert items[0].source_url == "https://example.org/tenders/FIX-JSON-001"
    assert items[0].documents[0].document_format is DocumentFormat.PDF
    assert items[1].documents == []
    assert items[0].raw_payload["id"] == "FIX-JSON-001"
    await fetcher.aclose()


async def test_http_json_connector_rejects_non_json(mock_fetcher):
    fetcher = mock_fetcher({"https://example.org/api": (200, "<html>nope</html>", "text/html")})
    connector = HTTPJSONConnector(fetcher)
    source = context(ConnectorType.HTTP, listing_paths=["/api"])
    response = await connector.fetch(source, DiscoveryTarget(url="https://example.org/api"))
    with pytest.raises(ParseError):
        await connector.parse(source, response)
    await fetcher.aclose()


# --- WordPress ------------------------------------------------------------


async def test_wordpress_connector_reads_the_rest_api(mock_fetcher, fixture_loader):
    payload = fixture_loader("wordpress_posts.json")
    fetcher = mock_fetcher(
        {"/wp-json/wp/v2/posts?per_page=50&page=1&_embed=1": (200, payload, "application/json")}
    )
    source = context(ConnectorType.WORDPRESS, post_type="posts", per_page=50, max_pages=1)
    connector = WordPressConnector(fetcher)
    items = [item async for item in connector.run(source)]

    assert len(items) == 2
    assert items[0].get("external_id") == "4271"
    assert "office furniture" in items[0].get("title")
    assert items[0].get("published_at") == "2026-09-01T06:00:00"
    assert items[0].documents[0].source_url.endswith("fixture-rfq-2026-014.pdf")
    # The XLSX bill of quantities is discovered too.
    assert any(doc.source_url.endswith(".xlsx") for doc in items[1].documents)
    await fetcher.aclose()


async def test_wordpress_connector_falls_back_to_html(mock_fetcher, fixture_loader):
    fetcher = mock_fetcher(
        {
            "/wp-json/wp/v2/posts?per_page=50&page=1&_embed=1": (
                404,
                '{"code":"rest_no_route"}',
                "application/json",
            ),
            "https://example.org/tenders": (200, fixture_loader("html_listing.html"), "text/html"),
        }
    )
    source = context(
        ConnectorType.WORDPRESS,
        post_type="posts",
        per_page=50,
        max_pages=1,
        html_fallback={
            "listing_paths": ["/tenders"],
            "item_selector": "table.tenders tbody tr",
            "field_selectors": {"title": "td:nth-child(2)"},
            "link_selector": "td:nth-child(2) a",
        },
    )
    connector = WordPressConnector(fetcher)
    items = [item async for item in connector.run(source)]

    assert len(items) == 3
    assert all(item.parser_metadata.get("fallback") == "html.listing" for item in items)
    await fetcher.aclose()


# --- PDF repository -------------------------------------------------------


async def test_pdf_connector_creates_one_item_per_pdf(mock_fetcher, fixture_loader):
    fetcher = mock_fetcher(
        {"https://example.org/adverts": (200, fixture_loader("pdf_repository.html"), "text/html")}
    )
    source = context(ConnectorType.PDF, listing_paths=["/adverts"])
    connector = PDFRepositoryConnector(fetcher)
    items = [item async for item in connector.run(source)]

    assert len(items) == 2  # the non-PDF link is ignored
    assert items[0].source_url.endswith("fixture-advert-2026-001.pdf")
    assert items[0].documents[0].document_format is DocumentFormat.PDF
    assert items[0].get("reference_number")
    await fetcher.aclose()


def test_first_meaningful_line_skips_noise():
    assert first_meaningful_line("x\n\nInvitation to tender for services") == (
        "Invitation to tender for services"
    )
    assert first_meaningful_line("") is None


# --- eTender (OCDS) -------------------------------------------------------


async def test_etender_connector_parses_ocds_releases(mock_fetcher, fixture_loader):
    fetcher = mock_fetcher(
        {
            "https://example.org/ocds/releases": (
                200,
                fixture_loader("etender_ocds_release.json"),
                "application/json",
            )
        }
    )
    source = context(ConnectorType.CUSTOM, listing_paths=["/ocds/releases"])
    connector = ETenderOCDSConnector(fetcher)
    items = [item async for item in connector.run(source)]

    assert len(items) == 2
    first = items[0]
    assert first.get("reference_number") == "FIX-2026-0001"
    assert first.get("external_id") == "ocds-fixture-0001"
    assert first.get("organization") == "TEST FIXTURE Department of Works"
    assert first.get("status") == "OPEN"
    assert first.get("estimated_value") == 8500000
    assert first.get("currency") == "ZAR"
    assert first.get("briefing_required") is True
    assert first.get("contact_email") == "scm@example.org"
    assert first.documents[0].source_url.endswith("fix-2026-0001.pdf")
    # Awards/contracts are preserved for later use rather than discarded.
    assert "awards" in first.raw_payload

    assert items[1].get("status") == "CANCELLED"
    assert items[1].get("procurement_type") == str(ProcurementType.RFQ)
    await fetcher.aclose()


async def test_etender_connector_requires_configured_endpoints(mock_fetcher):
    """No speculative URL is hard-coded: the endpoint must be configured."""
    fetcher = mock_fetcher({})
    connector = ETenderOCDSConnector(fetcher)
    with pytest.raises(ParseError):
        await connector.discover(context(ConnectorType.CUSTOM))
    await fetcher.aclose()
