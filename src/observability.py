"""Observability primitives: structured JSON logs, request IDs, latency timing.

Why structured logs: every production-grade service emits machine-parseable
logs so an aggregator (Loki, Datadog, ELK) can index by field. We standardize
on a flat JSON envelope with `ts`, `level`, `logger`, `event`, `request_id`,
`duration_ms`, and `attrs` (a sub-object for ad-hoc fields). The same shape
is emitted from middleware, the agent, and the LLM client.

Why a request_id contextvar: the API processes requests concurrently. A
contextvar makes the current request's ID available to any logger call
inside the request, without threading it through every function signature.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, ClassVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

# Type alias for the next-handler callable Starlette passes into dispatch().
NextHandler = Callable[[Request], Awaitable[Response]]

# ----------------------------------------------------------------------------
# Request-scoped context
# ----------------------------------------------------------------------------
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    """Return the request ID for the in-flight request, or '-' if none."""
    return _request_id_ctx.get()


# ----------------------------------------------------------------------------
# JSON formatter
# ----------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Render log records as a single-line JSON object.

    Standard fields:
        ts          ISO-8601 UTC timestamp
        level       lowercase log level
        logger      logger name
        event       message
        request_id  current request id (or '-')

    Extra fields passed via `logger.info("...", extra={"attrs": {...}})` are
    rendered under an `attrs` key. Exceptions render under `error`.
    """

    _RESERVED: ClassVar[set[str]] = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "asctime",
        "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": current_request_id(),
        }

        # Pick up any `extra` fields the caller passed.
        attrs: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            attrs[key] = _safe_jsonable(value)
        if attrs:
            # Surface the conventional `attrs` sub-dict if the caller used it.
            inner = attrs.pop("attrs", None)
            if isinstance(inner, dict):
                attrs.update(inner)
            payload["attrs"] = attrs

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent under repeated calls. Disables uvicorn's access logger because
    we emit our own per-request line from `RequestIdMiddleware`.
    """
    root = logging.getLogger()
    # Remove any pre-existing handlers (uvicorn installs its own).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json else logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    ))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet some noisy loggers in INFO mode.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("faiss.loader").setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# Middleware: assigns a request ID, times the request, emits an access log line.
# ----------------------------------------------------------------------------
class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a request ID + emit a structured access log per request."""

    HEADER = "x-request-id"

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._log = logging.getLogger("access")

    async def dispatch(self, request: Request, call_next: NextHandler) -> Response:
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex[:16]
        token = _request_id_ctx.set(rid)
        t0 = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            response.headers[self.HEADER] = rid
            return response
        finally:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            self._log.info(
                "http_request",
                extra={
                    "attrs": {
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "duration_ms": duration_ms,
                        "client": request.client.host if request.client else None,
                    }
                },
            )
            _request_id_ctx.reset(token)


# ----------------------------------------------------------------------------
# Security headers
# ----------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply conservative security headers to every response."""

    HEADERS: ClassVar[dict[str, str]] = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        # CSP is intentionally minimal: the API returns JSON, not HTML.
        "content-security-policy": "default-src 'none'",
    }

    async def dispatch(self, request: Request, call_next: NextHandler) -> Response:
        response = await call_next(request)
        for k, v in self.HEADERS.items():
            response.headers.setdefault(k, v)
        return response
