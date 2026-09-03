"""Unit tests for URL validation, canonicalisation and SSRF protection."""

from __future__ import annotations

import pytest

from app.errors import UnsafeURLError
from app.utils.urls import (
    filename_from_url,
    is_http_url,
    normalize_url,
    require_safe_url,
    same_registrable_host,
    validate_url,
    with_query,
)


@pytest.mark.parametrize(
    "url",
    ["https://example.org/tenders", "http://example.org", "https://example.org:8443/x"],
)
def test_accepts_public_http_urls(url):
    assert is_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file.pdf",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html;base64,PHN2Zz4=",
        "",
        None,
    ],
)
def test_rejects_non_http_schemes(url):
    assert not is_http_url(url)


def test_normalize_url_resolves_relative_and_strips_tracking():
    result = normalize_url("/tenders?utm_source=news&page=2#top", base="https://Example.ORG/x/y")
    assert result == "https://example.org/tenders?page=2"


def test_normalize_url_rejects_embedded_credentials():
    with pytest.raises(UnsafeURLError):
        normalize_url("https://user:pass@example.org/")


def test_normalize_url_drops_default_ports():
    assert normalize_url("https://example.org:443/a") == "https://example.org/a"
    assert normalize_url("http://example.org:80/a") == "http://example.org/a"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
    ],
)
def test_ssrf_guard_blocks_internal_destinations(url):
    check = validate_url(url, allow_private_networks=False)
    assert not check.ok
    assert check.reason


def test_ssrf_guard_blocks_disallowed_ports():
    check = validate_url("https://example.org:22/", allow_private_networks=False)
    assert not check.ok
    assert "port" in check.reason


def test_private_networks_allowed_only_when_explicitly_enabled():
    assert validate_url("http://127.0.0.1/x", allow_private_networks=True).ok


def test_require_safe_url_raises_for_unsafe_targets():
    with pytest.raises(UnsafeURLError):
        require_safe_url("http://127.0.0.1/x", allow_private_networks=False)


def test_same_registrable_host_ignores_www():
    assert same_registrable_host("https://www.example.org/a", "https://example.org/b")
    assert not same_registrable_host("https://example.org", "https://other.org")


def test_filename_from_url():
    assert filename_from_url("https://example.org/docs/bid.pdf") == "bid.pdf"
    assert filename_from_url("https://example.org/docs/") is None


def test_with_query_appends_parameters():
    assert with_query("https://example.org/a?x=1", page=2) == "https://example.org/a?x=1&page=2"
