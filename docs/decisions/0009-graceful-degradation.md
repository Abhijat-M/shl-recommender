# ADR 0009: Graceful degradation when the LLM is unavailable

* **Status:** Accepted
* **Date:** 2026-05-09
* **Related:** ADR-0008 (pluggable LLM provider)

## Context

The agent's "happy path" depends on the LLM router for every turn: it
extracts slots, classifies intent, and produces the search query. When
the LLM is fully unavailable — e.g., all Gemini models in the fallback
chain have hit their **20 requests/day/model** free-tier quotas, or a
a Gemini outage takes the chain down — the previous behavior was to emit a
canned clarifying question. That meant:

* The conversation looped on "What role are you hiring for?" forever.
* `compare` requests degraded to a meaningless clarify because we had no
  way to detect the compare intent without the LLM.
* The SHL evaluator's behavior probes saw an obviously-broken agent.

Live verification (see `scripts/verify_capabilities.py`) caught this
during back-to-back runs that exhausted the free tier.

## Decision

Add **two LLM-free fallback paths** that activate only when the router
LLM has failed:

1. **Local compare detection.**
   `agent/guardrails.py::extract_compare_targets` matches three patterns
   on the latest user message:
   - "what is the difference between A and B"
   - "compare A and B" / "comparing A vs B"
   - "A vs B" (bare form, only when the line matches end-to-end)

   When extraction succeeds, the orchestrator looks the targets up via
   the existing fuzzy matcher and emits the deterministic compare
   summary (`_deterministic_compare_summary`). No LLM call.

2. **Retrieval-only fallback.**
   When no compare is detected and the cumulative user text has at least
   2 words, run the hybrid retriever on the **concatenation of all user
   turns** and return the top 3-5 hits with a generic reply. This works
   because the retriever (FAISS + BM25 + RRF) is purely local and is the
   load-bearing primitive for grounding — the LLM "only" picks indices
   in the happy path.

   If retrieval still yields nothing, fall back to clarify (last resort).

The injection / off-topic / legal regex pre-filter still runs first, so
the retrieval-only path can never emit recs for an off-topic query.

## Alternatives considered

1. **Bubble the LLMError up as 5xx.** Rejected: the spec requires the
   service to always respond with the canonical schema. A 500 breaks the
   contract and the SHL evaluator scoring.
2. **Longer retry / slower backoff before falling back.** Insufficient:
   daily quotas don't reset within the per-call timeout window.
3. **Add more providers (OpenRouter, Mistral, Together) as further
   fallbacks.** Considered. Adds complexity (three more SDK shapes,
   three more sets of error semantics) for diminishing returns. Out of
   scope for now; pluggability via ADR-0008 makes this a 1-day add when
   needed.
4. **Cache the previous turn's slots and reuse them when the LLM fails.**
   Stateful — violates ADR-0001 (stateless API).

## Consequences

**Positive:**
- The agent remains useful when every configured LLM is down.
- Compare requests still produce grounded prose without the LLM.
- The SHL behavior-probe pass-rate improves under cumulative quota.
- Live verification (`scripts/verify_capabilities.py`) goes from 3/5 to
  5/5 when LLM-degraded conditions are induced.

**Negative:**
- The retrieval-only reply text is generic ("Here are SHL assessments
  that look most relevant to your query.") because we don't have the
  LLM to compose a contextual sentence.
- Slot-aware filtering (e.g., `test_types={P}`) is lost on the fallback;
  retrieval scores rank everything purely by query-document similarity.
- Compare extraction is regex-based; phrasings outside the three
  patterns ("how does X stack up against Y", "X compared with Y") fall
  through to retrieval-only and won't produce compare prose. Acceptable
  trade-off; the regex covers the SHL spec's example phrasings.

## Tests

- `tests/test_guardrails.py::test_compare_extraction` — 4 happy paths,
  4 negative cases (no false positives).
- `tests/test_orchestrator.py::test_router_failure_degrades_to_retrieval_only`
  — LLM raises, agent retrieves Java assessments anyway.
- `tests/test_orchestrator.py::test_router_failure_with_short_query_clarifies`
  — too-short query on LLM failure → clarify (not retrieval).
- `scripts/verify_capabilities.py` — end-to-end live verification of
  all five spec capabilities, 5/5 against the running service.
