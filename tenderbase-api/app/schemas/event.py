"""Opportunity event / change-feed schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.enums import EventType
from app.schemas.common import TenderBaseModel


class EventRead(TenderBaseModel):
    """A semantic change recorded against an opportunity."""

    id: UUID
    opportunity_id: UUID
    event_type: EventType
    field: str | None = None
    previous_value: Any | None = None
    new_value: Any | None = None
    description: str | None = None
    occurred_at: datetime
    created_at: datetime | None = None


class VersionRead(TenderBaseModel):
    """An immutable historical snapshot of an opportunity."""

    id: UUID
    opportunity_id: UUID
    version: int
    content_hash: str
    changed_fields: dict[str, Any] | None = None
    observed_at: datetime
    created_at: datetime | None = None


class VersionDetail(VersionRead):
    snapshot: dict[str, Any]
