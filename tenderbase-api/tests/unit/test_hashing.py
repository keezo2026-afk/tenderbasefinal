"""Unit tests for hashing, canonical serialisation and fingerprinting."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal

from app.utils.hashing import (
    canonical_json,
    contact_fingerprint,
    content_hash,
    fingerprint,
    normalize_for_fingerprint,
    sha256_bytes,
    sha256_chunks,
    sha256_stream,
    sha256_text,
    short_hash,
)


def test_sha256_helpers_agree():
    data = b"tenderbase"
    assert sha256_bytes(data) == sha256_text("tenderbase")
    assert sha256_stream(io.BytesIO(data)) == sha256_bytes(data)
    assert sha256_chunks([b"tender", b"base"]) == sha256_bytes(data)


def test_canonical_json_is_key_order_independent():
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_content_hash_is_stable_and_change_sensitive():
    payload = {
        "title": "Supply of solar panels",
        "closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
        "estimated_value": Decimal("1250000.00"),
    }
    first = content_hash(payload)
    assert first == content_hash(dict(payload))

    changed = {**payload, "title": "Supply of solar panels (amended)"}
    assert content_hash(changed) != first


def test_fingerprint_ignores_case_punctuation_and_time_of_day():
    base = {
        "title": "Supply  of Solar-Panels!",
        "organization": "Example Municipality",
        "closing_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
        "procurement_type": "RFQ",
    }
    variant = {
        "title": "supply of solar panels",
        "organization": "example  municipality",
        "closing_at": datetime(2026, 9, 15, 21, 59, tzinfo=UTC),
        "procurement_type": "RFQ",
    }
    assert fingerprint(base) == fingerprint(variant)


def test_fingerprint_distinguishes_different_opportunities():
    a = {
        "title": "Solar panels",
        "organization": "A",
        "closing_at": None,
        "procurement_type": "RFQ",
    }
    b = {"title": "Water pumps", "organization": "A", "closing_at": None, "procurement_type": "RFQ"}
    assert fingerprint(a) != fingerprint(b)


def test_normalize_for_fingerprint_handles_none_and_dates():
    assert normalize_for_fingerprint(None) == ""
    assert normalize_for_fingerprint(datetime(2026, 9, 15, tzinfo=UTC)) == "2026-09-15"


def test_contact_fingerprint_normalizes_phone_and_email():
    a = contact_fingerprint(
        name="Ms T Fixture", email="SCM@Example.org", phone="031 555 0100", organization="Muni"
    )
    b = contact_fingerprint(
        name="ms t fixture", email="scm@example.org", phone="+27315550100", organization="muni"
    )
    assert a == b


def test_short_hash_length():
    assert len(short_hash("value", 12)) == 12
