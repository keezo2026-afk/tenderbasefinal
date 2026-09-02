"""Consistent API error handling.

Clients always receive the same error envelope::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

In production, internal failures never leak stack traces, SQL, credentials or
filesystem paths — those go to the structured logs instead.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.errors import TenderBaseError
from app.logging import get_logger
from app.schemas.common import ErrorDetail, ErrorResponse

logger = get_logger("tenderbase.errors")

STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _response(
    *, status: int, code: str, message: str, request_id: str | None, details: dict | None = None
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=request_id, details=details)
    )
    return JSONResponse(
        status_code=status,
        content=payload.model_dump(mode="json", exclude_none=True),
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every exception handler to the application."""
    settings = get_settings()

    @app.exception_handler(TenderBaseError)
    async def _domain_error(request: Request, exc: TenderBaseError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("api.domain_error", code=exc.code, message=exc.message)
        return _response(
            status=exc.http_status,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _response(
            status=exc.status_code,
            code=STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
            message=str(exc.detail) if exc.detail else "Request failed",
            request_id=_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", []) if part != "body"),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type"),
            }
            for error in exc.errors()[:20]
        ]
        return _response(
            status=422,
            code="VALIDATION_ERROR",
            message="Request validation failed",
            request_id=_request_id(request),
            details={"errors": errors},
        )

    @app.exception_handler(SQLAlchemyError)
    async def _database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # Never surface SQL or connection strings to clients.
        logger.exception("api.database_error", error_type=type(exc).__name__)
        return _response(
            status=503,
            code="DATABASE_ERROR",
            message="A database error occurred. Please retry shortly.",
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("api.unhandled_error", error_type=type(exc).__name__)
        message = "An internal error occurred"
        details = None
        if not settings.is_production and settings.debug:
            details = {"exception": type(exc).__name__, "message": str(exc)[:500]}
        return _response(
            status=500,
            code="INTERNAL_ERROR",
            message=message,
            request_id=_request_id(request),
            details=details,
        )
