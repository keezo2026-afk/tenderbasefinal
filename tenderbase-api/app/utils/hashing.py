"""Hashing, content fingerprinting and stable canonical serialisation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, BinaryIO

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: Fields that constitute the secondary (layer 3) opportunity fingerprint.
FINGERPRINT_FIELDS = ("title", "organization", "closing_at", "procurement_type")

DEFAULT_CHUNK_SIZE = 1024 * 1024


def sha256_text(value: str) -> str:
    """SHA-256 of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """SHA-256 of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Stream-hash a binary file object without loading it fully into memory."""
    digest = hashlib.sha256()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_chunks(chunks: Iterable[bytes]) -> str:
    """Hash an iterable of byte chunks (streaming downloads)."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert a value into a deterministic JSON-serialisable form."""
    if isinstance(value, datetime):
        return value.astimezone(tz=value.tzinfo).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON encoding (sorted keys, no incidental whitespace)."""
    return json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Mapping[str, Any]) -> str:
    """Layer-2 dedup key: hash of the full canonical content of a record.

    Any change in any included field produces a different hash, which is what
    the version engine uses to decide "has this record changed?".
    """
    return sha256_text(canonical_json(payload))


def normalize_for_fingerprint(value: Any) -> str:
    """Aggressively normalize a value for fuzzy-stable fingerprinting."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        # Day precision: a time-of-day correction should not fork the identity.
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _WHITESPACE_RE.sub(" ", str(value)).strip().lower()
    return _NON_ALNUM_RE.sub("-", text).strip("-")


def fingerprint(payload: Mapping[str, Any], fields: Iterable[str] = FINGERPRINT_FIELDS) -> str:
    """Layer-3 dedup key: hash of aggressively normalized identity fields."""
    parts = [f"{field}={normalize_for_fingerprint(payload.get(field))}" for field in fields]
    return sha256_text("|".join(parts))


def contact_fingerprint(
    *, name: str | None, email: str | None, phone: str | None, organization: str | None
) -> str:
    """Stable identity for a contact record."""
    digits = re.sub(r"\D", "", phone or "")
    return sha256_text(
        "|".join(
            [
                normalize_for_fingerprint(name),
                (email or "").strip().lower(),
                digits[-9:] if digits else "",
                normalize_for_fingerprint(organization),
            ]
        )
    )


def short_hash(value: str, length: int = 12) -> str:
    """A short, stable, filesystem-safe hash (used for storage keys)."""
    return sha256_text(value)[:length]
