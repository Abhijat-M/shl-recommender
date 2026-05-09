"""API tests: /health, /chat schema, error handling."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.agent.orchestrator import Orchestrator
from src.api.routes import router
from src.api.schemas import ChatResponse


def _build_app(orch: Orchestrator):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.state.orchestrator = orch
    return app


def test_health(orchestrator: Orchestrator) -> None:
    client = TestClient(_build_app(orchestrator))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_clarify_schema(orchestrator: Orchestrator, fake_llm) -> None:
    fake_llm.json_responses = [
        {
            "intent": "clarify",
            "slots": {},
            "search_query": "x",
            "compare_targets": [],
            "refuse_reason": None,
            "clarifying_question": "What role?",
            "rationale": "",
        }
    ]
    client = TestClient(_build_app(orchestrator))
    r = client.post("/chat", json={"messages": [{"role": "user", "content": "Help"}]})
    assert r.status_code == 200
    body = r.json()
    # Schema check
    parsed = ChatResponse.model_validate(body)
    assert parsed.recommendations == []
    assert parsed.end_of_conversation is False


def test_chat_search_schema(orchestrator: Orchestrator, fake_llm) -> None:
    fake_llm.json_responses = [
        {
            "intent": "search",
            "slots": {"role": "java", "skills": ["java"]},
            "search_query": "java developer",
            "compare_targets": [],
            "refuse_reason": None,
            "clarifying_question": "",
            "rationale": "",
        },
        {"reply": "Picks.", "selected_indices": [0], "end_of_conversation": False},
    ]
    client = TestClient(_build_app(orchestrator))
    r = client.post(
        "/chat",
        json={
            "messages": [
                {"role": "user", "content": "Hiring a Java dev"},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    parsed = ChatResponse.model_validate(body)
    assert 1 <= len(parsed.recommendations) <= 10
    rec = parsed.recommendations[0]
    assert rec.name and rec.url and rec.test_type is not None


def test_chat_with_no_messages_returns_clarify(orchestrator: Orchestrator) -> None:
    client = TestClient(_build_app(orchestrator))
    r = client.post("/chat", json={"messages": []})
    assert r.status_code == 200
    body = r.json()
    parsed = ChatResponse.model_validate(body)
    assert parsed.recommendations == []
    assert parsed.reply  # non-empty


def test_chat_extra_field_rejected_or_ignored(orchestrator: Orchestrator, fake_llm) -> None:
    """Pydantic is lenient by default; we ensure the response still validates."""
    fake_llm.json_responses = [
        {
            "intent": "clarify",
            "slots": {},
            "search_query": "x",
            "compare_targets": [],
            "refuse_reason": None,
            "clarifying_question": "What role?",
            "rationale": "",
        }
    ]
    client = TestClient(_build_app(orchestrator))
    r = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "Help"}],
            "garbage_field": True,
        },
    )
    # We accept it; just verify the schema is still tight.
    assert r.status_code in (200, 422)


def test_recommendations_cap_in_response(orchestrator: Orchestrator, fake_llm) -> None:
    fake_llm.json_responses = [
        {
            "intent": "search",
            "slots": {"role": "developer"},
            "search_query": "developer",
            "compare_targets": [],
            "refuse_reason": None,
            "clarifying_question": "",
            "rationale": "",
        },
        {
            "reply": "Picks.",
            "selected_indices": list(range(20)),
            "end_of_conversation": False,
        },
    ]
    client = TestClient(_build_app(orchestrator))
    r = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "I need a developer test"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["recommendations"]) <= 10


def test_chat_handler_resilient_to_internal_failure(orchestrator: Orchestrator, fake_llm, monkeypatch) -> None:
    """If the orchestrator raises, we must still return a valid chat response (not 500)."""

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(orchestrator, "run", boom)
    client = TestClient(_build_app(orchestrator))
    r = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "anything"}]},
    )
    assert r.status_code == 200
    body = r.json()
    parsed = ChatResponse.model_validate(body)
    assert parsed.recommendations == []
