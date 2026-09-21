"""Domain exceptions and the RFC 9457 (problem+json) error contract.

Every HTTP error body produced by the API looks like:

    {
      "type": "https://errors.instamind.ai/tenant-isolation-violation",
      "title": "Workspace not found",
      "status": 404,
      "detail": "...",
      "code": "workspace_not_found",
      "errors": [{"field": "...", "code": "...", "message": "..."}],
      "instance": "/api/v1/contents/123",
      "trace_id": "..."
    }

Codes are stable and documented; clients branch on ``code``, never on prose.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_trace_id, logger


class AppError(Exception):
    """Base class for all domain errors mapped to HTTP responses."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"
    title: str = "Bad request"
    public: bool = True  # if False, `detail` is replaced by a generic message

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.errors = errors or []
        self.context = context or {}
        self.headers = headers or {}
        super().__init__(self.detail)

    def to_problem(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"https://errors.instamind.ai/{self.code.replace('_', '-')}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail if self.public else "An internal error occurred.",
            "code": self.code,
            "instance": str(request.url.path),
            "trace_id": get_trace_id(),
        }
        if self.errors:
            body["errors"] = self.errors
        return body


# --------------------------------------------------------------------------- #
# 4xx
# --------------------------------------------------------------------------- #
class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    title = "Validation failed"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    title = "Authentication required"

    def __init__(self, detail: str = "Authentication required.", **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kwargs)


class AuthorizationError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    title = "Not allowed"


class PermissionDenied(AuthorizationError):
    code = "permission_denied"
    title = "Insufficient permissions"


class TenantIsolationViolation(AuthorizationError):
    """Raised when a resource does not belong to the caller's workspace."""

    code = "resource_not_in_workspace"
    title = "Resource not found"
    public = False


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    title = "Not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    title = "Conflict"


class RateLimitExceeded(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limit_exceeded"
    title = "Too many requests"


class QuotaExceeded(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "quota_exceeded"
    title = "Plan limit reached"


class PreconditionFailed(AppError):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    code = "precondition_failed"
    title = "Precondition failed"


class PayloadTooLarge(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    title = "File too large"


class UnsupportedMediaType(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    title = "Unsupported file type"


class IllegalStateTransition(PreconditionFailed):
    code = "illegal_state_transition"
    title = "Illegal state transition"


class ExternalServiceError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    title = "Upstream service error"
    public = False


class MetaAPIError(ExternalServiceError):
    """A concrete error returned by the Meta Graph API.

    ``meta_code``/``meta_subcode`` are preserved because they drive retry and
    user-facing guidance (e.g. 190 = token expired, 4 / 17 / 32 / 613 = throttle).
    """

    code = "meta_api_error"
    title = "Instagram API error"
    public = True

    def __init__(
        self,
        detail: str,
        *,
        meta_code: int | None = None,
        meta_subcode: int | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
        is_transient: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(detail, **kwargs)
        self.meta_code = meta_code
        self.meta_subcode = meta_subcode
        self.error_type = error_type
        self.http_status = http_status
        self.is_transient = is_transient

    def to_problem(self, request: Request) -> dict[str, Any]:
        body = super().to_problem(request)
        body["meta"] = {
            "code": self.meta_code,
            "subcode": self.meta_subcode,
            "type": self.error_type,
            "retryable": self.is_transient,
        }
        return body


class ServiceUnavailable(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    title = "Service temporarily unavailable"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _log_problem(request: Request, status_code: int, code: str, detail: str) -> None:
    level = "warning" if status_code < 500 else "error"
    getattr(logger, level)(
        "api_error",
        code=code,
        status=status_code,
        path=str(request.url.path),
        method=request.method,
        detail=detail[:500],
        trace_id=get_trace_id(),
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    _log_problem(request, exc.status_code, exc.code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_problem(request),
        headers=exc.headers,
        media_type="application/problem+json",
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "rate_limit_exceeded",
    }
    code = code_map.get(exc.status_code, "http_error")
    detail = str(exc.detail)
    _log_problem(request, exc.status_code, code, detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"https://errors.instamind.ai/{code.replace('_', '-')}",
            "title": detail,
            "status": exc.status_code,
            "detail": detail,
            "code": code,
            "instance": str(request.url.path),
            "trace_id": get_trace_id(),
        },
        headers=getattr(exc, "headers", None),
        media_type="application/problem+json",
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(part) for part in err.get("loc", [])[1:]),
            "code": err.get("type", "value_error"),
            "message": err.get("msg", "Invalid value"),
        }
        for err in exc.errors()
    ]
    _log_problem(request, 422, "validation_error", "; ".join(e["field"] for e in errors))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "type": "https://errors.instamind.ai/validation-error",
            "title": "Validation failed",
            "status": 422,
            "detail": "One or more fields failed validation.",
            "code": "validation_error",
            "errors": errors,
            "instance": str(request.url.path),
            "trace_id": get_trace_id(),
        },
        media_type="application/problem+json",
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        path=str(request.url.path),
        method=request.method,
        trace_id=get_trace_id(),
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "type": "https://errors.instamind.ai/internal-error",
            "title": "Internal server error",
            "status": 500,
            "detail": "An unexpected error occurred. Reference the trace_id when contacting support.",
            "code": "internal_error",
            "instance": str(request.url.path),
            "trace_id": get_trace_id(),
        },
        media_type="application/problem+json",
    )
