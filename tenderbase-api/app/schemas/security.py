"""API key schemas.

Only non-secret representations exist here: a key's *prefix*, its scopes and
its lifecycle timestamps. The full key appears in exactly one response
(:class:`ApiKeyCreated`) and is never accepted back as a field, never stored and
never logged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator

from app.enums import API_KEY_SCOPES, ApiKeyStatus
from app.schemas.common import TenderBaseModel


class ApiKeyRead(TenderBaseModel):
    """A key as an operator sees it in a listing."""

    id: UUID
    name: str
    prefix: str = Field(description="Leading characters of the key — safe to display")
    status: ApiKeyStatus = ApiKeyStatus.ACTIVE
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    created_by: str | None = None
    notes: str | None = None


class ApiKeyIssueRequest(TenderBaseModel):
    """Input for minting a key (admin scope only)."""

    name: Annotated[str, Field(min_length=3, max_length=160)]
    scopes: list[str] | None = Field(
        default=None,
        description=(
            "Scopes to grant. Accepts the presets 'readonly', 'tenders', "
            f"'sources', 'admin'. Defaults to 'readonly'. Allowed: {', '.join(API_KEY_SCOPES)}"
        ),
    )
    expires_at: datetime | None = Field(
        default=None, description="ISO-8601 UTC timestamp; omit for a non-expiring key"
    )
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        label = value.strip()
        if len(label) < 3:
            raise ValueError("name must not be blank")
        return label


class ApiKeyCreated(TenderBaseModel):
    """**The only response that ever contains a raw key.**"""

    id: UUID
    name: str
    prefix: str
    key: str = Field(description="The complete API key. Shown once; not retrievable later.")
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    warning: str = Field(
        default="Store this key now. TenderBase keeps only a keyed digest and cannot resend it."
    )


class ApiKeyRevokeRequest(TenderBaseModel):
    reason: str | None = Field(default=None, max_length=300)


class ApiKeySummary(TenderBaseModel):
    """Counts used by ``GET /api-keys/summary`` (admin scope)."""

    active: int = 0
    revoked: int = 0
    expired: int = 0
