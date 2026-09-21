"""Structured logging + request tracing.

Every log line carries a ``trace_id`` (also returned to clients as
``X-Trace-Id`` and embedded in error bodies) so a support ticket can be mapped
to the exact server-side execution. Secrets are filtered before emission.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import settings

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_workspace_id: ContextVar[str] = ContextVar("workspace_id", default="-")
_user_id: ContextVar[str] = ContextVar("user_id", default="-")

SENSITIVE_KEYS = frozenset(
    {
        "password", "secret", "token", "access_token", "refresh_token",
        "authorization", "client_secret", "app_secret", "appsecret_proof",
        "api_key", "otp", "totp_secret", "signature", "cookie",
    }
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str | None = None) -> str:
    value = trace_id or new_trace_id()
    _trace_id.set(value)
    return value


def get_trace_id() -> str:
    return _trace_id.get()


def set_request_context(*, workspace_id: str | None = None, user_id: str | None = None) -> None:
    if workspace_id:
        _workspace_id.set(workspace_id)
    if user_id:
        _user_id.set(user_id)


def reset_request_context() -> None:
    _workspace_id.set("-")
    _user_id.set("-")


def _redact(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(k, v) for v in value for k in (key,)]
    return value


def context_filter(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    event_dict.setdefault("trace_id", get_trace_id())
    event_dict.setdefault("workspace_id", _workspace_id.get())
    event_dict.setdefault("user_id", _user_id.get())
    event_dict["service"] = "instamind-api"
    for key in list(event_dict.keys()):
        if key in {"event", "level", "timestamp", "logger", "service", "trace_id", "workspace_id", "user_id"}:
            continue
        event_dict[key] = _redact(key, event_dict[key])
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        context_filter,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.LOG_JSON:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")


logger = structlog.get_logger("instamind")
