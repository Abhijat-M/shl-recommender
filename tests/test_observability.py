"""Structured logging + request ID middleware tests."""

from __future__ import annotations

import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.observability import (
    JsonFormatter,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    current_request_id,
)


def test_json_formatter_emits_required_fields() -> None:
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    out = JsonFormatter().format(rec)
    parsed = json.loads(out)
    assert parsed["event"] == "hello world"
    assert parsed["level"] == "info"
    assert parsed["logger"] == "t"
    assert "ts" in parsed and "request_id" in parsed


def test_json_formatter_attrs_extra() -> None:
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x.py", lineno=1,
        msg="m", args=None, exc_info=None,
    )
    rec.__dict__["attrs"] = {"k": "v"}
    out = JsonFormatter().format(rec)
    parsed = json.loads(out)
    assert parsed["attrs"]["k"] == "v"


def test_configure_logging_idempotent() -> None:
    configure_logging(level="WARNING", json=True)
    configure_logging(level="WARNING", json=True)
    # Single handler installed, not duplicated.
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1


def test_request_id_middleware_assigns_and_echoes_header() -> None:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo() -> dict:
        return {"rid": current_request_id()}

    client = TestClient(app)
    # No incoming header -> server generates one.
    r1 = client.get("/echo")
    assert r1.headers.get("x-request-id")
    body = r1.json()
    assert body["rid"] == r1.headers["x-request-id"]

    # Incoming header -> server echoes it.
    r2 = client.get("/echo", headers={"X-Request-Id": "abcd1234"})
    assert r2.headers["x-request-id"] == "abcd1234"
    assert r2.json()["rid"] == "abcd1234"


def test_security_headers_applied() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/x")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_request_id_logs_emit_via_buffer() -> None:
    """The access logger emits one structured line per request."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    access_log = logging.getLogger("access")
    access_log.addHandler(handler)
    access_log.setLevel(logging.INFO)
    try:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/p")
        async def p() -> dict:
            return {"ok": True}

        client = TestClient(app)
        client.get("/p")
        out = buf.getvalue().splitlines()
        assert any("http_request" in line and '"path": "/p"' in line for line in out)
    finally:
        access_log.removeHandler(handler)
