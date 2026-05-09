"""LLM provider-agnostic interface.

The agent only depends on `LLMClient` — a structural protocol with two
methods (`complete_json`, `complete_text`). Concrete backends (Groq,
Gemini, fakes) all conform to this shape, which keeps the orchestrator and
tests free of any provider-specific imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LLMError(Exception):
    """Raised when an LLM call fails after all retries."""


class TransientLLMError(LLMError):
    """Subclass marker for errors worth retrying (timeouts, 429, 5xx).

    Backends raise this for retriable conditions; raise plain `LLMError`
    for permanent failures (4xx other than 429, malformed JSON, etc.) so
    they short-circuit instead of burning the retry budget.
    """


@dataclass(slots=True)
class LLMMessage:
    """One turn of input to an LLM. `role` is `"system" | "user" | "assistant"`."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@runtime_checkable
class LLMClient(Protocol):
    """Minimal interface every backend implements."""

    @property
    def model(self) -> str: ...

    async def complete_json(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        """Call the model in JSON mode. Returns the parsed dict.

        Raises `LLMError` on failure after retries.
        """
        ...

    async def complete_text(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> str:
        """Call the model and return the raw text reply."""
        ...
