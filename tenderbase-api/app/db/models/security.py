"""API authentication model: scoped, hashed, revocable API keys.

TenderBase is an API-only platform, so authentication is a machine-to-machine
API key rather than a user/session model. The design constraints are:

* **The raw key is never stored.** Only a keyed digest (HMAC-SHA256 over the
  key, using ``SECRET_KEY`` as a pepper) is persisted, so a database leak does
  not yield usable credentials, and the digest cannot be reversed or
  brute-forced against another deployment's key.
* **The raw key is shown exactly once**, at creation time.
* **A non-secret prefix is stored** (``key_prefix``) so operators can identify
  and rotate keys in listings and logs without exposing the secret.
* **Revocation and expiry are honoured on every request** — there is no cache
  that could keep a revoked key alive past one request.
* **Constant-time comparison** is used for the digest, and no API key is ever
  written to a log line (the log redactor drops anything matching
  ``api_key``/``authorization``).

No plaintext key, hash or pepper is ever returned by an API endpoint.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import JSONBType, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import ALL_READ_SCOPES, ApiKeyScope, ApiKeyStatus



class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single client credential with its scope set and lifecycle."""

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        Index("ix_api_keys_status", "status"),
        Index("ix_api_keys_key_prefix", "key_prefix"),
        CheckConstraint(
            "status IN ('ACTIVE','REVOKED','EXPIRED')",
            name="status_known",
        ),
        # The prefix must be long enough to be a useful identifier but is never
        # a secret; and only an explicitly revoked key carries a revocation stamp.
        CheckConstraint(
            "length(key_prefix) >= 6",
            name="key_prefix_min_length",
        ),
        CheckConstraint(
            "revoked_at IS NOT NULL OR status <> 'REVOKED'",
            name="revoked_keys_are_stamped",
        ),
    )

    #: Human label, e.g. "partner-acme-analytics". Never secret.
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: First characters of the raw key (e.g. ``tb_live_9f3c``) — an identifier
    #: safe to show in listings, audit logs and support threads.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Hex HMAC-SHA256 digest of the raw key. **The only representation stored.**
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApiKeyStatus.ACTIVE, server_default=str(ApiKeyStatus.ACTIVE)
    )
    #: JSON list of scope strings (see :data:`app.enums.API_KEY_SCOPES`).
    scopes: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- behaviour --------------------------------------------------------

    @property
    def is_valid(self) -> bool:
        """Whether this key may currently authenticate a request."""
        if self.status != str(ApiKeyStatus.ACTIVE):
            return False
        if self.expires_at is not None and self._as_utc(self.expires_at) <= self._now():
            return False
        return True

    @property
    def scope_set(self) -> frozenset[str]:
        return frozenset(self.scopes or ())

    def grants(self, required_scope: str | None) -> bool:
        """Whether this key satisfies ``required_scope`` (``admin`` grants all)."""
        if required_scope is None:
            return True
        scopes = self.scope_set
        if str(ApiKeyScope.ADMIN) in scopes:
            return True
        return required_scope in scopes

    @staticmethod
    def _now() -> datetime:
        from app.utils.dates import utcnow

        return utcnow()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        from app.utils.dates import ensure_utc

        return ensure_utc(value, assume_timezone="UTC")


#: Convenience scope presets accepted by ``scripts/manage_api_keys.py``.
SCOPE_PRESETS: dict[str, tuple[str, ...]] = {
    "readonly": ALL_READ_SCOPES,
    "tenders": (str(ApiKeyScope.READ_TENDERS),),
    "sources": (str(ApiKeyScope.READ_SOURCES), str(ApiKeyScope.READ_STATISTICS)),
    "admin": (str(ApiKeyScope.ADMIN),),
}
