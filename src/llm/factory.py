"""Construct an `LLMClient` from settings.

Default backend is **Gemini** (`GeminiClient`). The Groq backend is
kept on disk as a commented-out alternative — opt in by setting
`LLM_PROVIDER=groq` and `GROQ_API_KEY`. See ADR-0008 for the design.

Selection logic:
- `LLM_PROVIDER=gemini` (default when unset) -> `GeminiClient`.
- `LLM_PROVIDER=groq` (opt-in) -> `GroqClient`.
- Unknown value -> `ValueError`.
"""

from __future__ import annotations

import logging

from src.config import get_settings
from src.llm.base import LLMClient
from src.llm.gemini_client import GeminiClient

# Groq is preserved as a commented-out alternative. To re-enable, uncomment
# the import below and the matching branch in `make_llm()`.
from src.llm.groq_client import GroqClient

LOG = logging.getLogger(__name__)


def make_llm() -> LLMClient:
    """Return the configured `LLMClient`. Default: Gemini."""
    settings = get_settings()
    provider = (settings.llm_provider or "gemini").lower().strip()

    if provider == "gemini":
        if not settings.gemini_api_key:
            LOG.warning(
                "LLM_PROVIDER=gemini but GEMINI_API_KEY is empty; "
                "the LLM call will fail at runtime."
            )
        return GeminiClient()

    # --- alternative backend (off by default) ---
    if provider == "groq":
        # Groq is wired but not the default. Set GROQ_API_KEY to use it.
        return GroqClient()
    # --------------------------------------------

    raise ValueError(
        f"Unknown LLM_PROVIDER {provider!r}; expected 'gemini' (default) "
        f"or 'groq' (alternative)."
    )
