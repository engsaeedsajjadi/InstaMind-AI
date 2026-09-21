"""Request lifecycle middleware.

* trace id — propagated from ``X-Request-Id`` or generated, echoed back and
  embedded in every error body so a user report can be traced end to end.
* security headers — a baseline set (CSP is intentionally strict; the frontend
  is served separately so nothing here needs inline script).
* access log — one structured line per request, secrets redacted.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import logger, new_trace_id, reset_request_context, set_trace_id

TRACE_HEADER = "X-Request-Id"
_MAX_TRACE_LEN = 64

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    # The API serves JSON plus the Swagger UI. ``default-src 'self'`` keeps the
    # docs working; ``frame-ancestors 'none'`` blocks clickjacking. The Next.js
    # frontend sets its own, stricter CSP.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
}

# Paths that must not be logged at info level with bodies (and are noisy).
_QUIET_PATHS = {"/health/live", "/health/ready", "/metrics"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(TRACE_HEADER)
        trace_id = incoming[:_MAX_TRACE_LEN] if incoming else new_trace_id()
        set_trace_id(trace_id)
        reset_request_context()

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request_failed",
                method=request.method,
                path=str(request.url.path),
                duration_ms=duration_ms,
                trace_id=trace_id,
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers[TRACE_HEADER] = trace_id
        for key, value in SECURITY_HEADERS.items():
            # HSTS over plain HTTP would be a lie; only send it on TLS. Behind
            # a TLS-terminating proxy the forwarded proto is authoritative.
            if key == "Strict-Transport-Security":
                forwarded_proto = request.headers.get("x-forwarded-proto", "")
                is_https = request.url.scheme == "https" or forwarded_proto == "https"
                if not is_https:
                    continue
            # Never override a header the route set explicitly.
            response.headers.setdefault(key, value)

        if str(request.url.path) not in _QUIET_PATHS:
            logger.info(
                "request",
                method=request.method,
                path=str(request.url.path),
                status=response.status_code,
                duration_ms=duration_ms,
                client=request.client.host if request.client else None,
                trace_id=trace_id,
            )
        return response


def new_request_id() -> str:
    return str(uuid.uuid4())
