"""Gemini backend (Google AI generateContent REST API).

We talk to the REST endpoint directly via httpx rather than depending on
Google's SDK. The free tier (`gemini-2.0-flash`) is generous (15 RPM, 1M
TPM) and supports a JSON mode via `responseMimeType: application/json`.

Wire format:
    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={KEY}

We map our `LLMMessage` shape onto Gemini's:
- `system`     -> `systemInstruction.parts[].text`
- `user`       -> `contents[].role = "user"`
- `assistant`  -> `contents[].role = "model"`
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_settings
from src.llm.base import LLMError, LLMMessage, TransientLLMError

LOG = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClient:
    """Gemini `generateContent` wrapper conforming to `LLMClient`.

    Free-tier quota is **20 requests per day per model**. When the active
    model returns a daily-quota 429, we fall through the comma-separated
    list in `GEMINI_FALLBACK_MODELS` (each model has its own 20 RPD bucket).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_models: list[str] | None = None,
    ) -> None:
        settings = get_settings()
        key = api_key or settings.gemini_api_key
        if not key:
            LOG.warning("GEMINI_API_KEY is empty; calls will fail at runtime.")
        self._key = key
        self._model = model or settings.gemini_model
        if fallback_models is None:
            raw = settings.gemini_fallback_models or ""
            fallback_models = [m.strip() for m in raw.split(",") if m.strip()]
        # Build the model chain: primary first, fallbacks after, dedup.
        chain: list[str] = []
        for m in [self._model, *fallback_models]:
            if m and m not in chain:
                chain.append(m)
        self._model_chain = chain
        self._timeout = settings.llm_timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_chain(self) -> list[str]:
        return list(self._model_chain)

    # ----- public API -----
    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, TransientLLMError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    async def complete_json(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        raw = await self._call(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        # In JSON mode Gemini returns a clean JSON string. Defensive parse.
        try:
            return json.loads(_strip_codefence(raw))
        except json.JSONDecodeError as e:
            LOG.error("Gemini returned non-JSON: %r", raw[:500])
            raise LLMError(f"Gemini returned malformed JSON: {e}") from e

    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, TransientLLMError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    async def complete_text(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> str:
        return await self._call(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )

    # ----- internals -----
    async def _call(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        if not self._key:
            raise LLMError("GEMINI_API_KEY is not set.")
        payload = self._build_payload(messages, temperature, max_tokens, json_mode)
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=self._timeout + 5) as client:
            for model in self._model_chain:
                try:
                    resp = await client.post(
                        f"{_BASE_URL}/{model}:generateContent",
                        params={"key": self._key},
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
                except httpx.TimeoutException as e:
                    last_err = e
                    continue  # try next model on timeout
                if resp.status_code == 200:
                    return _extract_text(resp.json())
                # 429 with daily-quota signature -> try next model immediately
                # (no retry on this model). 429 RPM-style -> let tenacity retry.
                if resp.status_code == 429:
                    body = resp.text
                    if _is_daily_quota_exhausted(body):
                        LOG.warning(
                            "Gemini daily quota exhausted on %s; trying next.",
                            model,
                        )
                        last_err = LLMError(
                            f"Gemini daily quota exhausted on {model}"
                        )
                        continue
                    raise TransientLLMError(
                        f"Gemini 429 rate-limited on {model}: {body[:200]}"
                    )
                if resp.status_code >= 500:
                    raise TransientLLMError(
                        f"Gemini {resp.status_code} on {model}: {resp.text[:200]}"
                    )
                # 4xx other than 429: surface immediately.
                LOG.error("Gemini %d body=%s", resp.status_code, resp.text[:500])
                raise LLMError(
                    f"Gemini {resp.status_code} on {model}: {resp.text[:200]}"
                )
        # All models exhausted.
        if last_err is None:
            raise LLMError("Gemini call failed: no models available.")
        raise LLMError(f"All Gemini models exhausted: {last_err}")

    def _build_payload(
        self,
        messages: list[LLMMessage],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = (m.role or "user").lower()
            if role == "system":
                system_parts.append(m.content)
                continue
            mapped = "model" if role == "assistant" else "user"
            contents.append({"role": mapped, "parts": [{"text": m.content}]})

        gen_cfg: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"

        body: dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": ""}]}],
            "generationConfig": gen_cfg,
        }
        if system_parts:
            body["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }
        return body


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull the first candidate's text out of a Gemini response."""
    candidates = payload.get("candidates") or []
    if not candidates:
        # Surfaces blocked-by-safety responses too.
        feedback = payload.get("promptFeedback") or {}
        raise LLMError(f"Gemini returned no candidates: {json.dumps(feedback)[:200]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        finish = candidates[0].get("finishReason", "?")
        raise LLMError(f"Gemini returned empty text (finishReason={finish})")
    return text


def _is_daily_quota_exhausted(body: str) -> bool:
    """True if the 429 body indicates per-day-per-model quota is exhausted.

    Per-minute throttle has the same 429 status but different signature; we
    only fall through models for the daily-quota case.
    """
    # The free-tier daily metric ends in "free_tier_requests" with quotaId
    # "GenerateRequestsPerDayPerProjectPerModel-FreeTier". A substring match
    # on "PerDay" is robust to small wire-format changes.
    return "PerDay" in body or "free_tier_requests" in body


def _strip_codefence(text: str) -> str:
    """Some models occasionally wrap JSON in ```json … ```. Strip if present."""
    s = text.strip()
    if s.startswith("```"):
        s = s.lstrip("`")
        # Remove possible "json\n" prefix
        if s.lower().startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[: -3]
        return s.strip()
    return s
