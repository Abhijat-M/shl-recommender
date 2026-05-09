"""Shared test fixtures.

Tests are designed to run WITHOUT a live Groq key. We inject a `FakeLLM`
into the Orchestrator and exercise each branch deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.agent.orchestrator import Orchestrator
from src.config import get_settings
from src.llm.client import LLMMessage
from src.retrieval.catalog import Assessment, save_catalog
from src.retrieval.indexer import build
from src.retrieval.retriever import HybridRetriever


# ---------------------------------------------------------------------------
# Tiny fixture catalog (kept small so tests are fast).
# ---------------------------------------------------------------------------
def _seed_catalog() -> list[Assessment]:
    return [
        Assessment(
            name="Java 8 (New)",
            url="https://www.shl.com/products/product-catalog/view/java-8-new/",
            test_types=["K"],
            remote_testing=True,
            adaptive_irt=False,
            description="Multi-choice test that measures knowledge of Java 8 syntax, lambda expressions, streams API, and concurrency utilities.",
            job_levels=["Mid-Professional", "Professional Individual Contributor"],
            languages=["English (USA)"],
            assessment_length="Approximate Completion Time in minutes = 25",
        ),
        Assessment(
            name="Core Java (Entry Level) (New)",
            url="https://www.shl.com/products/product-catalog/view/core-java-entry-level-new/",
            test_types=["K"],
            remote_testing=True,
            adaptive_irt=False,
            description="Entry-level Java assessment covering OOP, control flow, collections, exception handling.",
            job_levels=["Entry-Level"],
            languages=["English (USA)"],
            assessment_length="Approximate Completion Time in minutes = 30",
        ),
        Assessment(
            name="Occupational Personality Questionnaire OPQ32r",
            url="https://www.shl.com/products/product-catalog/view/occupational-personality-questionnaire-opq32r/",
            test_types=["P"],
            remote_testing=True,
            adaptive_irt=False,
            description="A measure of behavioral preferences across 32 personality dimensions, designed for selection and development.",
            job_levels=["Manager", "Director", "Executive", "Mid-Professional"],
            languages=["English International", "French", "German"],
            assessment_length="Approximate Completion Time in minutes = 35",
        ),
        Assessment(
            # NB: real catalog uses U+2013 EN DASH; this exercises the
            # unicode-dash normalization in lookup_by_names.
            name="SHL Verify Interactive – Numerical Reasoning",  # noqa: RUF001
            url="https://www.shl.com/products/product-catalog/view/shl-verify-interactive-numerical-reasoning/",
            test_types=["A", "S"],
            remote_testing=True,
            adaptive_irt=True,
            description="Adaptive numerical reasoning test using interactive items.",
            job_levels=["Graduate", "Mid-Professional"],
            languages=["English International"],
            assessment_length="Approximate Completion Time in minutes = 18",
        ),
        Assessment(
            name="Customer Service Phone Simulation",
            url="https://www.shl.com/products/product-catalog/view/customer-service-phone-simulation/",
            test_types=["B", "S"],
            remote_testing=True,
            adaptive_irt=False,
            description="Realistic phone call simulation measuring listening, empathy, and resolution skills.",
            job_levels=["Entry-Level"],
            languages=["English (USA)"],
            assessment_length="Approximate Completion Time in minutes = 30",
        ),
        Assessment(
            name="Python (New)",
            url="https://www.shl.com/products/product-catalog/view/python-new/",
            test_types=["K"],
            remote_testing=True,
            adaptive_irt=False,
            description="Multi-choice test that measures Python knowledge: syntax, data types, comprehensions, OOP, error handling.",
            job_levels=["Mid-Professional"],
            languages=["English (USA)"],
            assessment_length="Approximate Completion Time in minutes = 20",
        ),
    ]


@pytest.fixture(scope="session")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a small index that all tests share."""
    work = tmp_path_factory.mktemp("idx")
    catalog_path = work / "catalog.json"
    index_dir = work / "index"
    save_catalog(_seed_catalog(), catalog_path)
    settings = get_settings()
    build(catalog_path, index_dir, settings.embedding_model)
    return index_dir


@pytest.fixture
def retriever(fixture_index: Path) -> HybridRetriever:
    return HybridRetriever(index_dir=fixture_index).load()


# ---------------------------------------------------------------------------
# FakeLLM lets us script the agent's behavior turn-by-turn.
# ---------------------------------------------------------------------------
@dataclass
class FakeLLM:
    """Drop-in replacement for `GroqClient` for tests.

    Configure with a queue of JSON dicts (for `complete_json`) and/or strings
    (for `complete_text`). Pops the next response on each call.
    """

    json_responses: list[dict] = field(default_factory=list)
    text_responses: list[str] = field(default_factory=list)
    seen_messages: list[list[LLMMessage]] = field(default_factory=list)
    on_call: Callable[[list[LLMMessage]], None] | None = None
    model: str = "fake-model"

    async def complete_json(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict:
        self.seen_messages.append(list(messages))
        if self.on_call:
            self.on_call(messages)
        if not self.json_responses:
            raise AssertionError("FakeLLM ran out of json_responses")
        await asyncio.sleep(0)  # respect cooperative scheduling
        return self.json_responses.pop(0)

    async def complete_text(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> str:
        self.seen_messages.append(list(messages))
        if self.on_call:
            self.on_call(messages)
        if not self.text_responses:
            raise AssertionError("FakeLLM ran out of text_responses")
        await asyncio.sleep(0)
        return self.text_responses.pop(0)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def orchestrator(retriever: HybridRetriever, fake_llm: FakeLLM) -> Orchestrator:
    """Wire an Orchestrator that uses the small index + a scriptable LLM."""
    orch = Orchestrator(retriever=retriever, llm=fake_llm)  # type: ignore[arg-type]
    orch.warm()
    return orch
