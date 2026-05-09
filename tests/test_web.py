"""Smoke tests for the static frontend mounted at /."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.agent.orchestrator import Orchestrator
from src.main import create_app


def _client(orch: Orchestrator) -> TestClient:
    app = create_app()
    app.state.orchestrator = orch
    return TestClient(app)


def test_index_served_at_root(orchestrator: Orchestrator) -> None:
    r = _client(orchestrator).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Title appears as "SHL · Assessment Recommender" (with separator),
    # so we assert on the substantive parts.
    assert "Assessment Recommender" in r.text
    assert "SHL" in r.text
    # The page references the API endpoints it will call.
    assert "/chat" in r.text and "/version" in r.text


def test_index_csp_loosened_for_html(orchestrator: Orchestrator) -> None:
    """JSON endpoints keep default-src 'none'; the HTML page must allow self."""
    r = _client(orchestrator).get("/")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp


def test_json_endpoints_keep_strict_csp(orchestrator: Orchestrator) -> None:
    r = _client(orchestrator).get("/health")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp


def test_index_security_headers_still_applied(orchestrator: Orchestrator) -> None:
    r = _client(orchestrator).get("/")
    # X-Content-Type-Options and friends must still apply on the HTML.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
