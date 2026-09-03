"""Shared response envelope, pagination and error schemas.

Every endpoint returns a consistent envelope::

    {"data": ..., "meta": {...}, "pagination": {...}}

and every error returns::

    {"error": {"code": ..., "message": ..., "request_id": ...}}
"""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

T = TypeVar("T")

_settings = get_settings()


class TenderBaseModel(BaseModel):
    """Base schema: ORM-friendly, strict about unknown input fields."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="ignore")


class PaginationParams(BaseModel):
    """Deterministic offset pagination. Limits are enforced server-side."""

    model_config = ConfigDict(extra="forbid")

    page: Annotated[int, Field(ge=1, le=10_000, description="1-based page number")] = 1
    page_size: Annotated[
        int,
        Field(
            ge=1,
            le=_settings.max_page_size,
            description=f"Items per page (max {_settings.max_page_size})",
        ),
    ] = _settings.default_page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class PaginationMeta(BaseModel):
    """Pagination block returned alongside every list response."""

    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @classmethod
    def build(cls, *, page: int, page_size: int, total_items: int) -> PaginationMeta:
        total_pages = ceil(total_items / page_size) if page_size and total_items else 0
        return cls(
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1 and total_pages > 0,
        )


class ResponseMeta(BaseModel):
    """Non-pagination response metadata."""

    request_id: str | None = None
    generated_at: datetime | None = None
    extra: dict[str, Any] | None = None


class DataResponse(BaseModel, Generic[T]):
    """Single-object envelope."""

    data: T
    meta: ResponseMeta | None = None


class ListResponse(BaseModel, Generic[T]):
    """Collection envelope with deterministic pagination."""

    data: list[T]
    pagination: PaginationMeta
    meta: ResponseMeta | None = None


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable description")
    request_id: str | None = Field(
        default=None, description="Correlates with the X-Request-ID header"
    )
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """The single error shape used by every endpoint."""

    error: ErrorDetail


class SortOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sort: str = "-published_at"


class HealthComponent(BaseModel):
    """One dependency's verdict.

    ``required`` distinguishes "this must work for us to serve traffic" from
    "nice to have": readiness only fails on unhealthy *required* components,
    while ``/health`` reports both so an operator sees the whole picture.
    """

    name: str
    status: str
    detail: str | None = None
    latency_ms: float | None = None
    required: bool = True


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    environment: str
    time: datetime
    components: list[HealthComponent] = Field(default_factory=list)


def paginated(
    items: list[T], *, page: int, page_size: int, total: int, request_id: str | None = None
) -> ListResponse[T]:
    """Helper constructing a :class:`ListResponse`."""
    return ListResponse[T](
        data=items,
        pagination=PaginationMeta.build(page=page, page_size=page_size, total_items=total),
        meta=ResponseMeta(request_id=request_id) if request_id else None,
    )
