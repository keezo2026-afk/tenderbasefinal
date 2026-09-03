"""Unit tests for text cleaning and extraction helpers."""

from __future__ import annotations

import pytest

from app.utils.text import (
    clean_text,
    collapse_whitespace,
    contains_any,
    extract_emails,
    extract_phones,
    normalize_reference_number,
    parse_money,
    slugify,
    truncate,
)


def test_clean_text_normalizes_whitespace_and_returns_none_for_empty():
    assert clean_text("  Supply   of\u00a0panels \n\n\n Next  ") == "Supply of panels\n\nNext"
    assert clean_text("   ") is None
    assert clean_text(None) is None


def test_clean_text_strips_control_characters_and_truncates():
    assert clean_text("bad\x00value") == "badvalue"
    assert len(clean_text("x" * 100, max_length=10)) == 10


def test_collapse_whitespace_and_truncate():
    assert collapse_whitespace("a\n b\tc") == "a b c"
    assert truncate("hello world again", 11).startswith("hello world")
    assert truncate("short", 100) == "short"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("scm/2026/001", "SCM/2026/001"),
        ("  SCM 2026 001 ", "SCM2026001"),
        ("SCM--2026//001", "SCM/2026/001"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_reference_number(raw, expected):
    assert normalize_reference_number(raw) == expected


def test_extract_emails_and_phones():
    blob = "Contact Ms T Fixture at SCM@Example.org or 031 555 0100 / +27 12 555 0199"
    assert extract_emails(blob) == ["scm@example.org"]
    phones = extract_phones(blob)
    assert "0315550100" in phones
    assert extract_emails(None) == []


@pytest.mark.parametrize(
    ("raw", "currency", "amount"),
    [
        ("R 1 250 000.00", "ZAR", "1250000.00"),
        ("ZAR 45,000", "ZAR", "45000"),
        ("Estimated value: R1,234.56 excl VAT", "ZAR", "1234.56"),
        ("no value published", None, None),
        (None, None, None),
    ],
)
def test_parse_money(raw, currency, amount):
    assert parse_money(raw) == (currency, amount)


def test_slugify_and_contains_any():
    assert slugify("KwaZulu-Natal Municipality!") == "kwazulu-natal-municipality"
    assert contains_any("Compulsory briefing session", ["briefing"])
    assert not contains_any(None, ["briefing"])
