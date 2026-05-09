"""GeminiClient tests — mock the REST endpoint with respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.llm import LLMError, LLMMessage
from src.llm.gemini_client import GeminiClient, _strip_codefence

GENAI = "https://generativelanguage.googleapis.com/v1beta/models"


def _ok(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": "STOP",
                }
            ]
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_complete_json_parses_response() -> None:
    route = respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=_ok('{"intent": "search", "rationale": "ok"}')
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    out = await c.complete_json([LLMMessage(role="user", content="hi")])
    assert out == {"intent": "search", "rationale": "ok"}
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    # role mapping: user -> "user"
    assert sent["contents"][0]["role"] == "user"


@pytest.mark.asyncio
@respx.mock
async def test_complete_text_round_trip() -> None:
    respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=_ok("hello back")
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    text = await c.complete_text([LLMMessage(role="user", content="say hi")])
    assert text == "hello back"


@pytest.mark.asyncio
@respx.mock
async def test_system_message_routed_to_systemInstruction() -> None:
    route = respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=_ok('{"ok": true}')
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    await c.complete_json(
        [
            LLMMessage(role="system", content="you are a helper"),
            LLMMessage(role="user", content="ping"),
        ]
    )
    sent = json.loads(route.calls.last.request.content)
    assert "systemInstruction" in sent
    assert "you are a helper" in sent["systemInstruction"]["parts"][0]["text"]
    # The system message must NOT appear in `contents`.
    for c_ in sent["contents"]:
        assert "you are a helper" not in c_["parts"][0]["text"]


@pytest.mark.asyncio
@respx.mock
async def test_assistant_role_mapped_to_model() -> None:
    route = respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=_ok('{"ok": true}')
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    await c.complete_json(
        [
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="assistant", content="hello"),
            LLMMessage(role="user", content="again"),
        ]
    )
    sent = json.loads(route.calls.last.request.content)
    roles = [c_["role"] for c_ in sent["contents"]]
    assert roles == ["user", "model", "user"]


@pytest.mark.asyncio
@respx.mock
async def test_empty_candidates_raises_llmerror() -> None:
    respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    with pytest.raises(LLMError):
        await c.complete_json([LLMMessage(role="user", content="x")])


@pytest.mark.asyncio
@respx.mock
async def test_429_is_retried_then_raised() -> None:
    respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    with pytest.raises(LLMError):
        await c.complete_json([LLMMessage(role="user", content="x")])


@pytest.mark.asyncio
@respx.mock
async def test_4xx_other_than_429_raises_immediately() -> None:
    route = respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    with pytest.raises(LLMError):
        await c.complete_json([LLMMessage(role="user", content="x")])
    # 400 is not retried — exactly one call.
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_strips_code_fence_around_json() -> None:
    respx.post(f"{GENAI}/gemini-2.0-flash:generateContent").mock(
        return_value=_ok('```json\n{"intent": "clarify"}\n```')
    )
    c = GeminiClient(api_key="fake", model="gemini-2.0-flash")
    out = await c.complete_json([LLMMessage(role="user", content="x")])
    assert out == {"intent": "clarify"}


def test_strip_codefence_helpers() -> None:
    assert _strip_codefence('{"a":1}') == '{"a":1}'
    assert _strip_codefence('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_codefence('```\n{"a":1}\n```') == '{"a":1}'


@pytest.mark.asyncio
async def test_no_key_raises() -> None:
    c = GeminiClient(api_key="", model="gemini-2.0-flash", fallback_models=[])
    with pytest.raises(LLMError):
        await c.complete_json([LLMMessage(role="user", content="x")])


@pytest.mark.asyncio
@respx.mock
async def test_daily_quota_falls_through_to_next_model() -> None:
    """When primary returns daily-quota 429, try the next model in the chain."""
    quota_429 = httpx.Response(
        429,
        json={
            "error": {
                "code": 429,
                "message": "Quota exceeded for metric",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaValue": "20",
                            }
                        ],
                    }
                ],
            }
        },
    )
    respx.post(f"{GENAI}/gemini-2.5-flash-lite:generateContent").mock(
        return_value=quota_429
    )
    respx.post(f"{GENAI}/gemini-flash-latest:generateContent").mock(
        return_value=_ok('{"intent": "search"}')
    )
    c = GeminiClient(
        api_key="fake",
        model="gemini-2.5-flash-lite",
        fallback_models=["gemini-flash-latest"],
    )
    out = await c.complete_json([LLMMessage(role="user", content="x")])
    assert out == {"intent": "search"}


@pytest.mark.asyncio
@respx.mock
async def test_per_minute_429_does_not_fall_through() -> None:
    """A non-daily 429 should retry on the same model, not switch models."""
    rpm_429 = httpx.Response(429, text="rate limited per minute")
    primary = respx.post(
        f"{GENAI}/gemini-2.5-flash-lite:generateContent"
    ).mock(return_value=rpm_429)
    fallback = respx.post(
        f"{GENAI}/gemini-flash-latest:generateContent"
    ).mock(return_value=_ok("ok"))
    c = GeminiClient(
        api_key="fake",
        model="gemini-2.5-flash-lite",
        fallback_models=["gemini-flash-latest"],
    )
    with pytest.raises(LLMError):
        await c.complete_text([LLMMessage(role="user", content="x")])
    # Tenacity retried the primary 3 times; fallback was never tried.
    assert primary.call_count == 3
    assert fallback.call_count == 0


def test_model_chain_dedups_and_preserves_order() -> None:
    c = GeminiClient(
        api_key="fake",
        model="a",
        fallback_models=["b", "a", "c", "b"],
    )
    assert c.model_chain == ["a", "b", "c"]
