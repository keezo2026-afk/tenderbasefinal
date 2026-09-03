"""Platform-wide change feed (events across all opportunities)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.dependencies import MetaDep, PaginationDep, SessionDep
from app.api.v1.routes.tenders import _event_to_schema
from app.db.models.opportunity import OpportunityEvent
from app.schemas.common import ListResponse, PaginationMeta
from app.schemas.event import EventRead
from app.utils.dates import ensure_utc

router = APIRouter(prefix="/events", tags=["events"])


@router.get(
    "",
    response_model=ListResponse[EventRead],
    summary="List change events",
    description=(
        "Cross-opportunity change feed — the basis for downstream alerting.\n\n"
        "Filter by `event_type` (e.g. `DEADLINE_CHANGED`, `DOCUMENT_ADDED`, "
        "`CANCELLED`), by `opportunity_id`, or by an `occurred_after` timestamp "
        "to poll incrementally. Newest first."
    ),
)
async def list_events(
    session: SessionDep,
    pagination: PaginationDep,
    meta: MetaDep,
    event_type: Annotated[str | None, Query(description="Filter by event type")] = None,
    opportunity_id: Annotated[UUID | None, Query()] = None,
    occurred_after: Annotated[
        str | None, Query(description="ISO-8601 timestamp for incremental polling")
    ] = None,
) -> ListResponse[EventRead]:
    stmt = select(OpportunityEvent)
    if event_type:
        stmt = stmt.where(OpportunityEvent.event_type == event_type.upper())
    if opportunity_id:
        stmt = stmt.where(OpportunityEvent.opportunity_id == opportunity_id)
    if occurred_after:
        from datetime import datetime

        try:
            moment = ensure_utc(
                datetime.fromisoformat(occurred_after.replace("Z", "+00:00")),
                assume_timezone="UTC",
            )
        except ValueError as exc:
            from app.errors import ValidationError

            raise ValidationError(
                "occurred_after must be an ISO-8601 timestamp",
                details={"occurred_after": occurred_after},
            ) from exc
        stmt = stmt.where(OpportunityEvent.occurred_at >= moment)

    total = int(
        (
            await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ).scalar_one()
    )
    stmt = (
        stmt.order_by(OpportunityEvent.occurred_at.desc(), OpportunityEvent.id.asc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    events = list((await session.execute(stmt)).scalars().all())
    return ListResponse[EventRead](
        data=[_event_to_schema(event) for event in events],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )
