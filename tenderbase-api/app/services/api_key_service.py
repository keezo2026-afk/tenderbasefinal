"""API key lifecycle: generation, hashing, verification, revocation.

Design notes (see also the API-key sections of ``docs/SECURITY.md`` and ``docs/DATABASE.md``):

* Raw keys are random 256-bit tokens with a readable prefix
  (``tb_<env>_<12 hex>_<43 base64url>``). Entropy comes from
  :func:`secrets.token_bytes`, never from a timestamp or counter.
* Storage holds only ``HMAC-SHA256(pepper, key)``. The pepper is
  ``API_KEY_PEPPER`` (or ``SECRET_KEY`` when unset), which means a stolen
  database dump contains nothing an attacker can test offline against another
  deployment.
* Verification uses a constant-time comparison after the indexed lookup, so a
  database that normalises or truncates strings cannot accept a near-match.
* Only the prefix is ever logged. Nothing in this module returns a raw key
  after creation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.security import SCOPE_PRESETS, ApiKey
from app.enums import API_KEY_SCOPES, ApiKeyScope, ApiKeyStatus
from app.errors import TenderBaseError, ValidationError
from app.logging import get_logger
from app.utils.dates import ensure_utc, utcnow

logger = get_logger("tenderbase.api_keys")

PREFIX_BYTES = 6  # 12 hex characters, shown in listings and logs
SECRET_BYTES = 32  # 256 bits of entropy
MIN_KEY_LENGTH = 20


class AuthenticationError(TenderBaseError):
    """Presented credential is missing, malformed or not accepted."""

    code = "UNAUTHORIZED"
    http_status = 401
    message = "A valid API key is required"


class InsufficientScopeError(TenderBaseError):
    code = "INSUFFICIENT_SCOPE"
    http_status = 403
    message = "This API key does not grant the required scope"


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """The one-time result of minting a key. ``raw_key`` is unrecoverable later."""

    raw_key: str
    prefix: str
    key_hash: str
    key_id: Any
    name: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_id": str(self.key_id),
            "name": self.name,
            "prefix": self.prefix,
            "scopes": list(self.scopes),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "raw_key": self.raw_key,
            "warning": "Store this value now — it cannot be retrieved again.",
        }


def generate_raw_key(environment: str = "live") -> tuple[str, str, str]:
    """Mint a key.

    Returns ``(raw_key, prefix, secret)`` — the hash is deliberately *not* returned
    here, so a caller cannot log a digest by accident; :func:`hash_api_key` is the only
    place that produces it, and it requires the pepper to be named explicitly.

    The environment label is cosmetic
    (``tb_live_``/``tb_test_``) so operators can tell deployments apart when
    pasting a key into a ticket — it is not a security property.
    """
    label = "".join(ch for ch in environment.lower() if ch.isalnum())[:8] or "api"
    prefix = f"tb_{label}_{secrets.token_hex(PREFIX_BYTES)}"
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"{prefix}_{secret}", prefix, secret


def hash_api_key(raw_key: str, *, pepper: str) -> str:
    """Keyed digest stored in ``api_keys.key_hash``. Deterministic, one-way."""
    if not raw_key:
        raise ValidationError("API key must not be empty", code="INVALID_KEY")
    return hmac.new(pepper.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def parse_scopes(values: list[str] | str | None) -> list[str]:
    """Validate and de-duplicate a scope list, expanding preset names.

    Unknown scopes are rejected rather than silently dropped: a typo in a
    grant ("read:tender") must not quietly create a key with *no* scopes that
    still looks valid to its owner.

    A comma may separate scopes whether they arrive as one string or as list items —
    ``--scopes read:tenders,read:statistics`` and ``--scopes read:tenders
    read:statistics`` are the same grant. An operator writing the first form into a
    CLI that only accepted the second would otherwise be told "Unknown scope",
    because argparse has already turned their argument into a list.
    """
    if values is None:
        return list(SCOPE_PRESETS["readonly"])
    if isinstance(values, str):
        values = [values]
    flat = [part.strip() for value in values for part in str(value).split(",")]
    values = [part for part in flat if part]
    resolved: list[str] = []
    for value in values:
        candidate = value.strip()
        if candidate.lower() in SCOPE_PRESETS:
            for scope in SCOPE_PRESETS[candidate.lower()]:
                if scope not in resolved:
                    resolved.append(scope)
            continue
        if candidate not in API_KEY_SCOPES:
            raise ValidationError(
                f"Unknown scope '{candidate}'",
                code="INVALID_SCOPE",
                details={"allowed": list(API_KEY_SCOPES), "presets": sorted(SCOPE_PRESETS)},
            )
        if candidate not in resolved:
            resolved.append(candidate)
    if not resolved:
        raise ValidationError("At least one scope is required", code="INVALID_SCOPE")
    return resolved


class ApiKeyService:
    """Database-backed operations on :class:`ApiKey` rows."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # -- minting ----------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        scopes: list[str] | str | None = None,
        expires_at: datetime | None = None,
        created_by: str | None = None,
        notes: str | None = None,
    ) -> IssuedKey:
        """Create a key. The raw value is returned once and never stored."""
        label = name.strip()
        if len(label) < 3:
            raise ValidationError("Key name must be at least 3 characters", code="INVALID_NAME")
        if expires_at is not None:
            expires_at = ensure_utc(expires_at, assume_timezone="UTC")
            if expires_at <= utcnow():
                raise ValidationError("expires_at must be in the future", code="INVALID_EXPIRY")

        resolved_scopes = parse_scopes(scopes)
        raw_key, prefix, _secret = generate_raw_key(self.settings.app_env)
        digest = hash_api_key(raw_key, pepper=self.settings.key_pepper)

        key = ApiKey(
            name=label,
            key_prefix=prefix,
            key_hash=digest,
            status=str(ApiKeyStatus.ACTIVE),
            scopes=resolved_scopes,
            expires_at=expires_at,
            created_by=created_by,
            notes=notes,
        )
        self.session.add(key)
        await self.session.flush()
        logger.info(
            "api_key.created", key_id=str(key.id), key_prefix=key.key_prefix, scopes=resolved_scopes
        )
        return IssuedKey(
            raw_key=raw_key,
            prefix=prefix,
            key_hash=digest,
            key_id=key.id,
            name=key.name,
            scopes=tuple(resolved_scopes),
            created_at=key.created_at,
            expires_at=key.expires_at,
        )

    # -- verification -----------------------------------------------------

    async def verify(self, raw_key: str | None, *, required_scope: str | None = None) -> ApiKey:
        """Resolve a presented key to its row, enforcing status and scope.

        Raises :class:`AuthenticationError` (401) for missing/unknown/expired/
        revoked keys and :class:`InsufficientScopeError` (403) when the key is
        valid but not entitled. Both are deliberately vague to clients about
        *why* a key was rejected beyond a stable code.
        """
        if not raw_key or len(raw_key) < MIN_KEY_LENGTH:
            raise AuthenticationError("Missing or malformed API key", code="API_KEY_MISSING")
        digest = hash_api_key(raw_key, pepper=self.settings.key_pepper)
        key = (
            (await self.session.execute(select(ApiKey).where(ApiKey.key_hash == digest)))
            .scalars()
            .first()
        )
        if key is None or not hmac.compare_digest(str(key.key_hash), digest):
            # Never echo the presented key, not even a suffix.
            logger.warning("api_key.unknown", key_prefix=raw_key[:16])
            raise AuthenticationError("Invalid API key", code="API_KEY_INVALID")
        if key.status == str(ApiKeyStatus.REVOKED):
            raise AuthenticationError("This API key has been revoked", code="API_KEY_REVOKED")
        if (
            key.expires_at is not None
            and ensure_utc(key.expires_at, assume_timezone="UTC") <= utcnow()
        ):
            if key.status != str(ApiKeyStatus.EXPIRED):
                key.status = str(ApiKeyStatus.EXPIRED)
                await self.session.flush()
            raise AuthenticationError("This API key has expired", code="API_KEY_EXPIRED")
        if key.status != str(ApiKeyStatus.ACTIVE):
            raise AuthenticationError("This API key is not active", code="API_KEY_INACTIVE")
        if required_scope and not key.grants(required_scope):
            raise InsufficientScopeError(
                f"This API key does not grant scope '{required_scope}'",
                code="INSUFFICIENT_SCOPE",
                details={"required": required_scope, "granted": sorted(key.scope_set)},
            )
        return key

    async def touch(self, key: ApiKey, *, client_ip: str | None = None) -> None:
        """Record usage for audit. Best-effort: never fails a request."""
        key.last_used_at = utcnow()
        if client_ip:
            key.last_used_ip = client_ip[:45]
        try:
            await self.session.flush()
        except Exception as exc:  # noqa: BLE001 - audit must not break the API
            logger.warning("api_key.touch_failed", key_id=str(key.id), error=str(exc))

    # -- operator operations ---------------------------------------------

    async def revoke(self, key_id: str, *, reason: str | None = None) -> ApiKey:
        key = await self._get(key_id)
        key.status = str(ApiKeyStatus.REVOKED)
        key.revoked_at = utcnow()
        key.revoked_reason = (reason or "Revoked by operator")[:300]
        await self.session.flush()
        logger.info("api_key.revoked", key_id=str(key.id), key_prefix=key.key_prefix)
        return key

    async def list_keys(self, *, include_revoked: bool = True) -> list[ApiKey]:
        stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).limit(500)
        if not include_revoked:
            stmt = stmt.where(ApiKey.status != str(ApiKeyStatus.REVOKED))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, key_id: str) -> ApiKey:
        """Fetch one key's metadata by id (never by key value)."""
        return await self._get(key_id)

    async def _get(self, key_id: str) -> ApiKey:
        from uuid import UUID

        try:
            identifier = UUID(str(key_id))
        except ValueError as exc:
            raise ValidationError("key_id must be a UUID", code="INVALID_ID") from exc
        key = (
            (await self.session.execute(select(ApiKey).where(ApiKey.id == identifier)))
            .scalars()
            .first()
        )
        if key is None:
            from app.errors import NotFoundError

            raise NotFoundError("API key not found", code="API_KEY_NOT_FOUND")
        return key

    async def stats(self) -> dict[str, int]:
        rows = (
            await self.session.execute(select(ApiKey.status, func.count()).group_by(ApiKey.status))
        ).all()
        return {str(status): int(count) for status, count in rows}


def read_only_scopes() -> list[str]:
    """The default grant for a data-consumer key (all reads, no admin)."""
    return [
        str(ApiKeyScope.READ_TENDERS),
        str(ApiKeyScope.READ_SOURCES),
        str(ApiKeyScope.READ_DOCUMENTS),
        str(ApiKeyScope.READ_STATISTICS),
        str(ApiKeyScope.READ_GEOGRAPHY),
    ]
