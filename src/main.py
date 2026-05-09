"""FastAPI application entry point.

Wires up the production middleware stack:

    [client]
        |
        v
    SecurityHeadersMiddleware
        |
        v
    RequestIdMiddleware    <-- assigns x-request-id, emits structured access log
        |
        v
    RateLimitMiddleware    <-- in-memory token bucket on /chat
        |
        v
    CORSMiddleware         <-- restricted by ALLOWED_ORIGINS env var
        |
        v
    Routes (/health, /ready, /version, /metrics, /chat)

On startup we eagerly warm the orchestrator (loads the index, fetches the
embedding model) so the first /chat doesn't pay the cold-start cost.

The orchestrator is attached to `app.state` so the request handlers can
resolve it through a `Depends`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src import __version__
from src.agent.orchestrator import Orchestrator
from src.api.routes import router
from src.config import get_settings
from src.observability import (
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
)
from src.rate_limit import RateLimitMiddleware, TokenBucketLimiter

API_DESCRIPTION = """
SHL Conversational Assessment Recommender.

A grounded conversational agent that recommends SHL Individual Test Solutions
through dialogue. Given a hiring manager's vague intent, it clarifies, refines,
recommends, compares, and refuses off-topic / legal / prompt-injection input.

**Stateless** — pass the full conversation history with every `POST /chat`.

**Schema-strict** — the response shape is fixed: `reply`, `recommendations`
(0 or 1-10 items), `end_of_conversation`. The schema is non-negotiable.

**Catalog-grounded** — every URL returned comes from a scraped snapshot of
SHL's Individual Test Solutions catalog. Items not in the catalog are
filtered out before the response is serialized.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging, warm the orchestrator on startup, log shutdown."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json=os.getenv("LOG_JSON", "1") != "0")
    log = logging.getLogger("startup")
    log.info("bootstrapping", extra={"attrs": {"version": __version__}})

    orch: Orchestrator | None = None
    try:
        orch = Orchestrator()
        orch.warm()
        log.info(
            "orchestrator_ready",
            extra={"attrs": {"catalog_size": len(orch._retriever.catalog)}},
        )
    except Exception:
        log.exception("orchestrator_warm_failed")
        orch = None

    app.state.orchestrator = orch
    try:
        yield
    finally:
        log.info("shutdown")


def _allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if raw == "*" or not raw:
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    """Build the FastAPI app. Importable by uvicorn (`src.main:app`)."""
    app = FastAPI(
        title="SHL Conversational Assessment Recommender",
        version=__version__,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        contact={"name": "SHL Recommender"},
        openapi_tags=[
            {"name": "chat", "description": "Conversational endpoint (stateless)."},
            {"name": "probes", "description": "Health, readiness, version, metrics."},
        ],
    )

    # ---- middleware (outermost first; they execute in reverse-add order) ----
    # CORS is the innermost middleware so it runs after rate limit / req-id.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "x-request-id"],
        expose_headers=["x-request-id"],
        max_age=3600,
    )
    # Rate limit only applies to /chat (skip list inside the middleware).
    app.add_middleware(
        RateLimitMiddleware,
        limiter=TokenBucketLimiter(
            capacity=float(os.getenv("RATE_LIMIT_BURST", "10")),
            refill_per_sec=float(os.getenv("RATE_LIMIT_RPS", "1")),
        ),
    )
    # Request ID + access logs go around everything else.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(router)

    # ---- Static frontend ----
    # A single-page chat UI (HTML+CSS+JS, no build step) is served at "/".
    # The CSP for HTML is loosened (`default-src 'self'`) only for this route
    # so inline styles/scripts work. JSON endpoints keep `default-src 'none'`.
    web_root = Path(__file__).resolve().parent / "web"
    index_html = web_root / "index.html"
    if index_html.exists():
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(
                index_html,
                media_type="text/html",
                headers={
                    "content-security-policy": (
                        "default-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; "
                        "script-src 'self' 'unsafe-inline'; "
                        "connect-src 'self'"
                    )
                },
            )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )
