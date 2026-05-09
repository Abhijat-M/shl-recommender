"""LLM backends. Public surface is provider-agnostic.

The default backend is **Gemini** (`GeminiClient`). `GroqClient` is
re-exported here as a commented-out alternative — it is functional and
fully tested but not selected unless you set `LLM_PROVIDER=groq`.

Importing pattern:
    from src.llm import LLMClient, LLMMessage, LLMError, make_llm
"""

from src.llm.base import LLMClient, LLMError, LLMMessage, TransientLLMError
from src.llm.factory import make_llm
from src.llm.gemini_client import GeminiClient

# Alternative backend, kept reachable for opt-in via LLM_PROVIDER=groq.
from src.llm.groq_client import GroqClient

__all__ = [
    "GeminiClient",
    "GroqClient",      # alternative backend (off by default)
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "TransientLLMError",
    "make_llm",
]
