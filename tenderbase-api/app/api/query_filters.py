"""Query-filter construction that reports bad client input as a client error.

FastAPI validates declared bodies and query parameters, but the list endpoints here
assemble their filter model *inside a dependency*. A value Pydantic rejects — a status
that is not one of the enum's members, `published_after` later than `published_before`
— then raised ``pydantic.ValidationError`` from the middle of the dependency graph,
where nothing translates it: the client received **500 INTERNAL_ERROR** for a typo in a
query string, and the incident looked like a server fault.

Going through the application's own error type instead keeps the single documented error
envelope and reports the problem as what it is, with one entry per offending field.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.errors import ValidationError

FilterModel = TypeVar("FilterModel", bound=BaseModel)


def parse_query_filter(model: type[FilterModel], **values: Any) -> FilterModel:
    """Build a filter model from raw query parameters.

    Deliberately scoped to *inputs*. Response models are still constructed directly: a
    response model failing validation means our own data or schema is wrong, and that
    should stay a 500 rather than be relabelled as something the client can fix.
    """
    try:
        return model(**values)
    except PydanticValidationError as exc:
        raise ValidationError(
            "One or more query parameters are invalid",
            details={"errors": _field_errors(exc)},
        ) from exc


def _field_errors(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Shape Pydantic's errors like the request-validation handler shapes its own.

    Both paths must produce the same ``details.errors`` entries, or a client has to
    learn two shapes for the same class of mistake depending on which layer caught it.
    ``GET /api/v1/tenders?page=0`` is rejected by FastAPI and reports ``query.page``;
    ``?status=BOGUS`` is rejected here, so the location prefix is added by hand to make
    the two indistinguishable. A cross-field failure has no single parameter to name and
    reports the bare location, ``query``.
    """
    errors = []
    for item in exc.errors()[:20]:
        parts = [str(part) for part in item.get("loc", ()) if part != "query"]
        errors.append(
            {
                "field": ".".join(["query", *parts]),
                "message": item.get("msg", "invalid value"),
                "type": item.get("type"),
            }
        )
    return errors
