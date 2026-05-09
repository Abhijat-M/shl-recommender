"""FastAPI routes: GET /health, GET /ready, GET /version, GET /metrics, POST /chat.

All endpoints are stateless. /chat is the only one that performs LLM/retrieval
work; the rest are cheap probes used by deploy platforms and dashboards.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from src import __version__
from src.agent.orchestrator import Orchestrator
from src.api.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    ReadyResponse,
    Recommendation,
    VersionResponse,
)
from src.config import get_settings
from src.metrics import get_metrics

LOG = logging.getLogger(__name__)
router = APIRouter()


def _orchestrator(request: Request) -> Orchestrator:
    """Resolve the orchestrator off `app.state`. 503 if startup didn't warm it."""
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="agent not ready")
    return orch


# ----------------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------------
@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["probes"],
    summary="Liveness probe",
    description=(
        "Always returns `{\"status\": \"ok\"}` if the process is up. "
        "Does **not** confirm the index is loaded; use `/ready` for that."
    ),
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadyResponse,
    tags=["probes"],
    summary="Readiness probe",
    description=(
        "Returns `ready` only when (1) the FAISS+BM25 index is loaded and "
        "(2) the LLM API key is configured. Returns 503 otherwise."
    ),
)
async def ready(request: Request) -> ReadyResponse:
    settings = get_settings()
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(
            status_code=503,
            detail=ReadyResponse(
                status="not_ready",
                catalog_size=0,
                llm_configured=bool(settings.gemini_api_key or settings.groq_api_key),
            ).model_dump(),
        )
    return ReadyResponse(
        status="ready",
        catalog_size=len(orch._retriever.catalog),
        llm_configured=bool(settings.gemini_api_key or settings.groq_api_key),
    )


@router.get(
    "/version",
    response_model=VersionResponse,
    tags=["probes"],
    summary="Build version info",
)
async def version() -> VersionResponse:
    settings = get_settings()
    # Show whichever model the active provider would use.
    provider = (settings.llm_provider or "").lower().strip()
    if not provider:
        provider = "gemini" if settings.gemini_api_key else "groq"
    model = settings.gemini_model if provider == "gemini" else settings.groq_model
    return VersionResponse(
        name="shl-recommender",
        version=__version__,
        model=f"{provider}:{model}",
        embedding_model=settings.embedding_model,
    )


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    tags=["probes"],
    summary="Prometheus metrics",
    description="Process-wide metrics in Prometheus text exposition format.",
)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(get_metrics().render_prometheus(), media_type="text/plain; version=0.0.4")


# ----------------------------------------------------------------------------
# Chat
# ----------------------------------------------------------------------------
@router.post(
    "/chat",
    response_model=ChatResponse,
    tags=["chat"],
    summary="One conversational turn",
    description=(
        "Stateless. Pass the **full** message history each call; the service "
        "stores no per-conversation state. Returns the agent's next reply, "
        "an optional shortlist (empty when clarifying/refusing, 1-10 items "
        "when committed), and an `end_of_conversation` flag."
    ),
    responses={
        200: {"description": "Agent processed the turn (may be a clarifying or refusing reply)."},
        429: {"description": "Rate limit hit. Includes a `Retry-After` header."},
        503: {"description": "Service is starting up; retry after a few seconds."},
    },
)
async def chat(
    body: ChatRequest,
    orch: Annotated[Orchestrator, Depends(_orchestrator)],
) -> ChatResponse:
    settings = get_settings()
    metrics = get_metrics()
    started = time.perf_counter()

    # Honor the spec's turn cap (max 8 turns including user & assistant).
    history = (
        body.messages[-settings.max_turns:]
        if len(body.messages) > settings.max_turns
        else body.messages
    )

    plain_history = [m.model_dump() for m in history]
    try:
        result = await orch.run(plain_history)
        metrics.inc("shl_chat_requests_total", labels={"status": "ok"})
        metrics.inc("shl_chat_intent_total", labels={"intent": result.intent or "unknown"})
        metrics.inc("shl_chat_recs_returned_total", value=len(result.recommendations))
    except Exception:
        LOG.exception("chat handler failed")
        metrics.inc("shl_chat_requests_total", labels={"status": "error"})
        # Fail open: a generic clarifying reply rather than a 500. The spec
        # requires the agent to always emit a valid response shape; better to
        # ask for context than to break the conversation with an HTTP error.
        return ChatResponse(
            reply=(
                "Sorry - I hit an internal issue. Could you tell me the role "
                "and seniority you're hiring for?"
            ),
            recommendations=[],
            end_of_conversation=False,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    metrics.observe("shl_chat_duration_ms", duration_ms)

    LOG.info(
        "chat",
        extra={
            "attrs": {
                "intent": result.intent,
                "n_recs": len(result.recommendations),
                "duration_ms": duration_ms,
                "rationale": result.rationale[:120],
            }
        },
    )

    return ChatResponse(
        reply=result.reply,
        recommendations=[
            Recommendation(name=r.name, url=r.url, test_type=r.test_type)
            for r in result.recommendations
        ],
        end_of_conversation=result.end_of_conversation,
    )


__all__ = ["router"]
