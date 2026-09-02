"""Version engine.

When a record is seen again, the version engine decides whether anything
meaningful changed, writes an immutable ``opportunity_versions`` snapshot and
emits semantic ``opportunity_events``. History is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.models.opportunity import (
    OpportunityEvent,
    OpportunityVersion,
    ProcurementOpportunity,
)
from app.enums import EventType, OpportunityStatus
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import to_iso, utcnow

#: Fields compared when deciding whether a record changed.
TRACKED_FIELDS: tuple[str, ...] = (
    "reference_number",
    "title",
    "description",
    "procurement_type",
    "status",
    "organization",
    "published_at",
    "closing_at",
    "estimated_value",
    "currency",
    "submission_method",
    "submission_url",
    "submission_address",
    "briefing_required",
    "briefing_compulsory",
    "briefing_date",
    "briefing_location",
    "source_url",
    "canonical_url",
)

#: Field → event mapping for changes that deserve their own semantic event.
FIELD_EVENTS: dict[str, EventType] = {
    "closing_at": EventType.DEADLINE_CHANGED,
    "briefing_date": EventType.BRIEFING_CHANGED,
    "briefing_location": EventType.BRIEFING_CHANGED,
    "status": EventType.STATUS_CHANGED,
    "submission_method": EventType.SUBMISSION_CHANGED,
    "submission_url": EventType.SUBMISSION_CHANGED,
    "submission_address": EventType.SUBMISSION_CHANGED,
}


@dataclass(slots=True)
class VersionDecision:
    """The outcome of comparing an incoming record with a stored one."""

    changed: bool
    changed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[tuple[EventType, str | None, Any, Any, str]] = field(default_factory=list)

    @property
    def field_names(self) -> list[str]:
        return sorted(self.changed_fields)


def _comparable(value: Any) -> Any:
    """Reduce a value to a stable, comparable JSON-friendly form."""
    if isinstance(value, datetime):
        return to_iso(value)
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


class VersionEngine:
    """Computes diffs, snapshots and events."""

    def diff(
        self, existing: ProcurementOpportunity, incoming: NormalizedOpportunity
    ) -> VersionDecision:
        """Compare a stored record with a freshly normalized one."""
        decision = VersionDecision(changed=False)

        for name in TRACKED_FIELDS:
            old = _comparable(getattr(existing, name, None))
            new = _comparable(getattr(incoming, name, None))
            # Never regress a known value to NULL because one scrape missed it.
            if new is None and old is not None:
                continue
            if old == new:
                continue
            decision.changed = True
            decision.changed_fields[name] = {"from": old, "to": new}

            event_type = FIELD_EVENTS.get(name, EventType.OPPORTUNITY_UPDATED)
            if name == "status":
                event_type = self._status_event(str(new))
            decision.events.append(
                (event_type, name, old, new, f"{name} changed from {old!r} to {new!r}")
            )

        old_documents = {doc.source_url for doc in (existing.documents or [])}
        new_documents = {doc.source_url for doc in incoming.documents}
        if added := sorted(new_documents - old_documents):
            decision.changed = True
            decision.changed_fields["documents_added"] = {"from": [], "to": added}
            decision.events.append(
                (
                    EventType.DOCUMENT_ADDED,
                    "documents",
                    None,
                    added,
                    f"{len(added)} document(s) added",
                )
            )
        if removed := sorted(old_documents - new_documents):
            # A document disappearing from a listing is recorded, not deleted.
            decision.changed_fields["documents_removed"] = {"from": removed, "to": []}
            decision.events.append(
                (
                    EventType.DOCUMENT_REMOVED,
                    "documents",
                    removed,
                    None,
                    f"{len(removed)} document(s) no longer listed",
                )
            )

        return decision

    def _status_event(self, new_status: str) -> EventType:
        parsed = OpportunityStatus.parse(new_status)
        if parsed is OpportunityStatus.CANCELLED:
            return EventType.CANCELLED
        if parsed is OpportunityStatus.EXTENDED:
            return EventType.EXTENDED
        if parsed is OpportunityStatus.AWARDED:
            return EventType.AWARD_POSTED
        return EventType.STATUS_CHANGED

    def snapshot(self, record: NormalizedOpportunity) -> dict[str, Any]:
        """Canonical snapshot persisted in ``opportunity_versions``."""
        payload = record.hashable_payload()
        payload["published_at"] = to_iso(record.published_at)
        payload["closing_at"] = to_iso(record.closing_at)
        payload["briefing_date"] = to_iso(record.briefing_date)
        payload["estimated_value"] = (
            str(record.estimated_value) if record.estimated_value is not None else None
        )
        payload["content_hash"] = record.content_hash
        payload["fingerprint"] = record.fingerprint
        payload["data_quality"] = str(record.data_quality)
        return payload

    def build_version(
        self,
        *,
        opportunity: ProcurementOpportunity,
        record: NormalizedOpportunity,
        version_number: int,
        changed_fields: dict[str, Any] | None,
        source_run_id: Any = None,
        observed_at: datetime | None = None,
    ) -> OpportunityVersion:
        return OpportunityVersion(
            opportunity_id=opportunity.id,
            version=version_number,
            content_hash=record.content_hash,
            snapshot=self.snapshot(record),
            changed_fields=changed_fields or None,
            source_run_id=source_run_id,
            observed_at=observed_at or utcnow(),
        )

    def build_events(
        self,
        *,
        opportunity: ProcurementOpportunity,
        decision: VersionDecision,
        version: OpportunityVersion | None,
        occurred_at: datetime | None = None,
    ) -> list[OpportunityEvent]:
        """Materialise the semantic events for a change decision."""
        moment = occurred_at or utcnow()
        events: list[OpportunityEvent] = []
        for event_type, field_name, old, new, description in decision.events:
            events.append(
                OpportunityEvent(
                    opportunity_id=opportunity.id,
                    event_type=str(event_type),
                    version_id=version.id if version is not None else None,
                    field=field_name,
                    previous_value={"value": old} if old is not None else None,
                    new_value={"value": new} if new is not None else None,
                    description=description[:1000],
                    occurred_at=moment,
                )
            )
        return events

    def creation_event(
        self, opportunity: ProcurementOpportunity, *, occurred_at: datetime | None = None
    ) -> OpportunityEvent:
        return OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type=str(EventType.OPPORTUNITY_CREATED),
            description="Opportunity first observed",
            occurred_at=occurred_at or utcnow(),
        )
