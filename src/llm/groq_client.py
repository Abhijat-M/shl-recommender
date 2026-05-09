"""Groq backend — KEPT AS A COMMENTED-OUT ALTERNATIVE (not the default).

The active LLM backend is Gemini (`src/llm/gemini_client.py`). This Groq
client is preserved on disk so a future maintainer can flip back with a
one-line change, but it is NOT wired into the default factory output.

To re-enable, set:
    LLM_PROVIDER=groq
    GROQ_API_KEY=gsk_...
The factory in `src/llm/factory.py` will then return this client.

Implementation: synchronous Groq SDK wrapped in `asyncio.to_thread`.
Retries on transient errors with exponential backoff via `tenacity`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

from groq import APIError, APITimeoutError, Groq, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_settings
from src.llm.base import LLMError, LLMMessage

LOG = logging.getLogger(__name__)


class GroqClient:
    """Groq chat-completions wrapper conforming to `LLMClient`."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        key = api_key or settings.groq_api_key
        if not key:
            LOG.warning("GROQ_API_KEY is empty; calls will fail at runtime.")
        self._client = Groq(api_key=key) if key else None
        self._model = model or settings.groq_model
        self._timeout = settings.llm_timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APITimeoutError, APIError, asyncio.TimeoutError)
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
        client = self._client
        if client is None:
            raise LLMError("GROQ_API_KEY is not set.")

        def _call() -> str:
            resp = client.chat.completions.create(
                model=self._model,
                messages=cast(Any, [m.to_dict() for m in messages]),
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                timeout=self._timeout,
            )
            return resp.choices[0].message.content or "{}"

        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_call), timeout=self._timeout + 5)
        except TimeoutError as e:
            raise LLMError(f"LLM call timed out after {self._timeout}s") from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            LOG.error("LLM returned non-JSON: %r", raw[:500])
            raise LLMError(f"LLM returned malformed JSON: {e}") from e

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APITimeoutError, APIError, asyncio.TimeoutError)
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
        client = self._client
        if client is None:
            raise LLMError("GROQ_API_KEY is not set.")

        def _call() -> str:
            resp = client.chat.completions.create(
                model=self._model,
                messages=cast(Any, [m.to_dict() for m in messages]),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self._timeout,
            )
            return resp.choices[0].message.content or ""

        try:
            return await asyncio.wait_for(asyncio.to_thread(_call), timeout=self._timeout + 5)
        except TimeoutError as e:
            raise LLMError(f"LLM call timed out after {self._timeout}s") from e
