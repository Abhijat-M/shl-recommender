"""Backward-compat shim — public types live in `src.llm` now.

Kept so existing test imports (`from src.llm.client import ...`) continue
to work. New code should import from `src.llm` directly.
"""

from src.llm.base import LLMClient, LLMError, LLMMessage
from src.llm.factory import make_llm
from src.llm.gemini_client import GeminiClient
from src.llm.groq_client import GroqClient

__all__ = [
    "GeminiClient",
    "GroqClient",
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "make_llm",
]
