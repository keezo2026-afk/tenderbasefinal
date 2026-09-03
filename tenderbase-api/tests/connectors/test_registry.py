"""Connector registry tests."""

from __future__ import annotations

import pytest

import app.connectors  # noqa: F401 - registers the built-ins
from app.connectors.base import ProcurementConnector
from app.connectors.registry import (
    build_connector,
    get_connector_class,
    list_connectors,
    register_connector,
    registered_keys,
)
from app.enums import ConnectorType
from app.errors import ConnectorNotRegisteredError

EXPECTED_KEYS = {
    "http.json",
    "html.listing",
    "wordpress.rest",
    "pdf.repository",
    "browser.playwright",
    "custom.etender_ocds",
}


def test_all_builtin_connectors_are_registered():
    assert EXPECTED_KEYS.issubset(set(registered_keys()))


def test_lookup_by_key():
    assert get_connector_class("html.listing").key == "html.listing"


@pytest.mark.parametrize(
    ("connector_type", "expected_key"),
    [
        (ConnectorType.HTTP, "http.json"),
        (ConnectorType.HTML, "html.listing"),
        (ConnectorType.WORDPRESS, "wordpress.rest"),
        (ConnectorType.PDF, "pdf.repository"),
        (ConnectorType.BROWSER, "browser.playwright"),
    ],
)
def test_default_connector_per_type(connector_type, expected_key):
    assert get_connector_class(None, connector_type).key == expected_key


def test_unknown_key_raises_a_clear_error():
    with pytest.raises(ConnectorNotRegisteredError):
        get_connector_class("does.not.exist")
    with pytest.raises(ConnectorNotRegisteredError):
        get_connector_class()


def test_build_connector_injects_the_fetcher():
    sentinel = object()
    connector = build_connector("html.listing", fetcher=sentinel)
    assert connector.fetcher is sentinel


def test_registry_description_is_api_ready():
    described = {item["key"]: item for item in list_connectors()}
    for key in EXPECTED_KEYS:
        entry = described[key]
        assert entry["name"] and entry["description"]
        assert entry["connector_type"] in {t.value for t in ConnectorType}
    assert described["browser.playwright"]["requires_browser"] is True


def test_duplicate_keys_are_rejected():
    with pytest.raises(ValueError):

        @register_connector()
        class Duplicate(ProcurementConnector):
            key = "html.listing"

            async def discover(self, source): ...
            async def fetch(self, source, target): ...
            async def parse(self, source, response): ...


def test_connectors_must_declare_a_key():
    with pytest.raises(ValueError):

        @register_connector()
        class Anonymous(ProcurementConnector):
            async def discover(self, source): ...
            async def fetch(self, source, target): ...
            async def parse(self, source, response): ...
