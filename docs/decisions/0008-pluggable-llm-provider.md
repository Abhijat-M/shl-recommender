# ADR 0008: Gemini LLM provider with multi-model fallback

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The agent makes 1-2 LLM calls per `/chat` turn (router classification +
optional generator selection). Two qualities matter for free-tier
deployment:

1. **Sustained throughput.** The SHL evaluator runs many traces back to
   back; a per-minute or per-day quota that's easy to exhaust will
   degrade the pass rate.
2. **JSON-mode reliability.** The agent depends on parseable JSON from
   the model on every turn — provider quirks here cost real time to
   debug.

Google's Gemini family on the free tier offers:
- Generous context (1M tokens),
- Native JSON output via `responseMimeType: application/json`,
- Sub-second median latency for the Flash variants,
- A small per-day quota per model (~20 RPD), but **separate per-model**
  quotas — so a 4-model chain effectively gives ~80 RPD.

## Decision

A small `LLMClient` Protocol in `src/llm/base.py` defines the surface
the orchestrator depends on:

```python
class LLMClient(Protocol):
    @property
    def model(self) -> str: ...
    async def complete_json(self, messages, *, temperature, max_tokens) -> dict: ...
    async def complete_text(self, messages, *, temperature, max_tokens) -> str: ...
```

The default backend is `GeminiClient`, which talks to
`generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
over `httpx`. It accepts a primary model plus a comma-separated list of
fallbacks (`GEMINI_FALLBACK_MODELS`); on a daily-quota 429 it rolls
forward to the next model. Per-minute 429s still retry on the same
model (those reset within the request budget).

Default chain (each entry has its own ~20 RPD bucket):
- `gemini-flash-latest` (primary)
- `gemini-2.5-flash`
- `gemini-2.0-flash`
- `gemini-2.5-flash-lite`

A `TransientLLMError` subclass marks errors worth retrying (timeouts,
429, 5xx). The base `LLMError` signals permanent failures (4xx other
than 429, malformed JSON) so the retry decorator short-circuits.

Because the orchestrator depends only on the Protocol, swapping in a
different backend is a one-file change — useful if the Gemini free
tier ever stops being viable. We did not feel the need to ship a
second backend in production; the abstraction itself is the insurance.

## Alternatives considered

1. **OpenRouter as a free aggregator.** OpenRouter's free tier is 200
   RPD total across all models — too tight for development + eval.
   Direct Gemini gives ~80 RPD per project on the chain.
2. **Pull in `google-generativeai` SDK.** The REST API has a stable
   shape (`generateContent` v1beta) and we already depend on httpx; the
   SDK would add ~3 transitive deps for no functional gain.
3. **Single primary model, no fallbacks.** Tested. A single 20-RPD
   bucket exhausts during eval; the chain raised our effective ceiling
   ~4× without any code complexity beyond a simple loop.

## Consequences

**Positive:**
- ~80 effective RPD on free tier (vs ~20 with a single model).
- One narrow file (`gemini_client.py`, ~170 lines) is the only place
  that knows wire details.
- The `LLMClient` protocol makes test fakes trivial — `FakeLLM` doesn't
  inherit from anything; structural typing is enough.
- Adding a new backend (Mistral, OpenRouter, Anthropic, etc.) is
  ~120 lines.

**Negative:**
- Gemini's `system` → `systemInstruction` mapping and code-fence
  stripping live in the backend module. Acceptable — the orchestrator
  stays clean.
- Free-tier daily quota is a real cliff. ADR-0009 covers the
  retrieval-only fallback for the case where the entire chain is spent.
