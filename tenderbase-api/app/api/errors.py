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

from app.config import Settings, get_settings
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


def error_response(
    *,
    status: int,
    code: str,
    message: str,
    request_id: str | None,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the one error envelope the API uses, for handlers *and* middleware.

    Middleware cannot simply raise :class:`app.errors.TenderBaseError` and let the
    registered handler format it: Starlette's exception handling lives inside the
    user middleware stack, so an exception raised by ``PublicRateLimitMiddleware``
    escapes as a 500 — a rate-limited client would be told the server broke, and
    any monitoring keyed on 429 would miss the event entirely. Returning the
    response here keeps the envelope identical on both paths.
    """
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=request_id, details=details)
    )
    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status,
        content=payload.model_dump(mode="json", exclude_none=True),
        headers=response_headers or None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every exception handler to the application."""
    # The app's own settings object: a test (or a second mounted app) that built
    # its application with explicit settings must not inherit the process global.
    settings: Settings = getattr(app.state, "settings", None) or get_settings()

    @app.exception_handler(TenderBaseError)
    async def _domain_error(request: Request, exc: TenderBaseError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("api.domain_error", code=exc.code, message=exc.message)
        return error_response(
            status=exc.http_status,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details or None,
            # e.g. Retry-After on 429. Only the error's own declared headers.
            headers=exc.headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Authentication/rate-limit failures raised as bare HTTPExceptions (e.g.
        # by dependencies that must not import the domain layer) keep their
        # status code and any headers the framework attached.
        headers = dict(getattr(exc, "headers", None) or {})
        return error_response(
            status=exc.status_code,
            code=STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
            message=str(exc.detail) if exc.detail else "Request failed",
            request_id=_request_id(request),
            headers=headers or None,
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
        return error_response(
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
        return error_response(
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
        return error_response(
            status=500,
            code="INTERNAL_ERROR",
            message=message,
            request_id=_request_id(request),
            details=details,
        )
