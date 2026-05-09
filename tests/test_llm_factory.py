"""make_llm() factory selects the right backend by settings."""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.llm import GeminiClient, GroqClient, make_llm


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_explicit_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert isinstance(make_llm(), GeminiClient)


def test_explicit_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert isinstance(make_llm(), GroqClient)


def test_groq_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a Groq key set, Gemini is selected unless LLM_PROVIDER=groq."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    assert isinstance(make_llm(), GeminiClient)


def test_default_is_gemini_even_without_explicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini is the active default. Groq is opt-in only."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    assert isinstance(make_llm(), GeminiClient)


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    with pytest.raises(ValueError):
        make_llm()
