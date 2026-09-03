"""Text cleaning and light extraction helpers used across normalization."""

from __future__ import annotations

import re
import unicodedata
from typing import overload

_WS_RE = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# South African phone numbers: 011 123 4567 / +27 11 123 4567 / (011) 123-4567
_PHONE_RE = re.compile(r"(?:\+27|0)(?:[\s\-().]?\d){8,11}")
_REFERENCE_CLEAN_RE = re.compile(r"[^A-Z0-9/\-]")
_MONEY_RE = re.compile(
    r"(?P<currency>R|ZAR|USD|EUR|\$|€|£)\s?(?P<amount>\d[\d\s,.]*\d|\d)", re.IGNORECASE
)


def clean_text(value: str | None, *, max_length: int | None = None) -> str | None:
    """Normalize unicode, collapse whitespace, strip control characters.

    Returns ``None`` for empty input — never an empty string, so that "absent"
    is unambiguous in the database.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _MULTI_NEWLINE_RE.sub("\n\n", text).strip()
    if not text:
        return None
    if max_length is not None and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def collapse_whitespace(value: str) -> str:
    """Collapse all runs of whitespace (including newlines) into single spaces."""
    return re.sub(r"\s+", " ", value).strip()


@overload
def truncate(value: str, length: int, suffix: str = "…") -> str: ...


@overload
def truncate(value: None, length: int, suffix: str = "…") -> None: ...


def truncate(value: str | None, length: int, suffix: str = "…") -> str | None:
    """Truncate on a word boundary where possible.

    A `str` in means a `str` out (rather than `str | None`), which is what callers
    building already-validated strings need; the optional form exists for columns that
    may be NULL.
    """
    if value is None or len(value) <= length:
        return value
    cut = value[:length]
    # Keep whole words: only trim back when the cut lands mid-word.
    if not value[length].isspace() and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + suffix


def extract_emails(value: str | None) -> list[str]:
    """Return unique lowercase e-mail addresses found in the text."""
    if not value:
        return []
    seen: dict[str, None] = {}
    for match in _EMAIL_RE.findall(value):
        seen.setdefault(match.lower(), None)
    return list(seen)


def extract_phones(value: str | None) -> list[str]:
    """Return unique South-African-style phone numbers found in the text."""
    if not value:
        return []
    seen: dict[str, None] = {}
    for match in _PHONE_RE.findall(value):
        cleaned = re.sub(r"[\s\-().]", "", match)
        if 9 <= len(cleaned.lstrip("+")) <= 13:
            seen.setdefault(cleaned, None)
    return list(seen)


def normalize_reference_number(value: str | None) -> str | None:
    """Normalize a tender reference for comparison (uppercase, tidy separators).

    The original reference is always stored separately; this is a *comparison*
    key only.
    """
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).upper().strip()
    text = re.sub(r"\s+", "", text)
    text = _REFERENCE_CLEAN_RE.sub("", text)
    text = re.sub(r"[/\-]{2,}", "/", text).strip("/-")
    return text or None


def parse_money(value: str | None) -> tuple[str | None, str | None]:
    """Extract ``(currency, amount)`` as strings from free text.

    Returns ``(None, None)`` when no amount is confidently present. Ambiguous
    values are *not* guessed.
    """
    if not value:
        return None, None
    match = _MONEY_RE.search(value)
    if not match:
        return None, None
    symbol = match.group("currency").upper()
    currency = {"R": "ZAR", "$": "USD", "€": "EUR", "£": "GBP"}.get(symbol, symbol)
    raw_amount = match.group("amount").replace(" ", "")
    # "1,234.56" → 1234.56 ; "1.234,56" → 1234.56 ; "1,234" → 1234
    if "," in raw_amount and "." in raw_amount:
        if raw_amount.rfind(",") > raw_amount.rfind("."):
            raw_amount = raw_amount.replace(".", "").replace(",", ".")
        else:
            raw_amount = raw_amount.replace(",", "")
    elif "," in raw_amount:
        decimals = raw_amount.rsplit(",", 1)[-1]
        raw_amount = (
            raw_amount.replace(",", ".") if len(decimals) == 2 else raw_amount.replace(",", "")
        )
    try:
        float(raw_amount)
    except ValueError:
        return None, None
    return currency, raw_amount


def slugify(value: str, *, max_length: int = 200) -> str:
    """ASCII slug suitable for stable identifiers."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_length].strip("-")


def contains_any(haystack: str | None, needles: list[str]) -> bool:
    """Case-insensitive "contains any keyword" test."""
    if not haystack:
        return False
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)
