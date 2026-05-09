# Testing Strategy

## Goals

1. Verify the service honors the SHL spec at the API boundary
   (schema-strict, recommendations 1-10, etc.).
2. Verify each agent intent (clarify / search / refine / compare / refuse)
   produces the expected response shape.
3. Verify guardrails block known prompt-injection patterns and reject URLs
   that aren't in the catalog.
4. Verify retrieval returns sensible top-K for representative queries.
5. Run **without** a live Gemini API key — tests must be deterministic and
   cheap (CI-friendly).

## Test layers

```
            ┌─────────────────────────────┐
            │   API tests (test_api.py)    │  TestClient hits /health, /chat,
            │                              │  validates ChatResponse schema
            └────────────┬─────────────────┘
                         │
            ┌────────────▼─────────────────┐
            │ Orchestrator tests           │  Each intent path with
            │   (test_orchestrator.py)     │  scriptable FakeLLM
            └────────────┬─────────────────┘
                         │
   ┌──────────┬──────────┴─────────────┬───────────────┬────────────┐
   ▼          ▼                        ▼               ▼            ▼
guardrails  retrieval                cache         metrics    rate_limit
test_*.py   test_*.py              test_*.py     test_*.py    test_*.py
```

### `tests/conftest.py` — fixtures shared across the suite

* `fixture_index` — builds a 6-item FAISS+BM25 index in a tmp dir, once per
  session. Avoids a 5-second rebuild per test.
* `retriever` — a `HybridRetriever` loaded from `fixture_index`.
* `fake_llm` — an `LLMClient` stand-in. Tests queue `dict` responses on
  `fake_llm.json_responses`; each call pops the next.
* `orchestrator` — wired with `retriever` + `fake_llm`. Already warmed.

### `tests/test_api.py` — HTTP boundary

Uses FastAPI's `TestClient` against an in-process app. Asserts:
- `/health` → 200 `{"status":"ok"}`.
- `/chat` returns a `ChatResponse`-shaped body for clarify, search, empty.
- Response always validates against the Pydantic schema.
- Recommendations cap at 10 even if the LLM returns 20 indices.
- Internal exceptions are caught — handler returns a valid response, not 500.

### `tests/test_orchestrator.py` — agent behavior

Each test scripts the FakeLLM with the JSON the router/generator would have
returned, then asserts the agent's `AgentTurnResult`:

* Vague first turn → `intent="clarify"`, no recs.
* Sufficient context → `intent="search"`, 1-10 recs, all URLs from catalog.
* Off-topic → `intent="refuse"`, no recs, "SHL" mentioned in reply.
* Injection ("ignore previous instructions") → caught by the local regex
  prefilter; FakeLLM is **never called** (the test asserts this).
* Legal ("is it legal to test for X") → caught by the local legal regex,
  no LLM call (ADR-0009).
* Compare with valid targets → `intent="compare"`, no recs, prose reply.
* Compare with unknown targets → fall back to clarify.
* Router LLM raises with a substantive query → orchestrator does
  retrieval-only fallback and returns real catalog recs (ADR-0009).
* Router LLM raises with a trivially-short query → falls back to clarify.
* Generator returns 20 indices → final recs capped at 10.

### `tests/test_retrieval.py` — hybrid search

* Java query returns Java assessments.
* Python query returns Python (New).
* Filtering by `test_types={"P"}` returns only items containing P.
* `lookup_by_names` finds exact and fuzzy matches (e.g. "OPQ32r").
* Empty query short-circuits to `[]`.

### `tests/test_guardrails.py` — defensive layer

* Each known injection phrase matches `is_obviously_injection`.
* Clean inputs (job descriptions, comparisons) do NOT match.
* Off-topic regex catches weather/recipe/movie/sports.
* Legal regex catches `is it legal`, `EEOC`, `discrimination`,
  `lawsuit`, `GDPR`, `Title VII`, etc. (10 cases; 4 negatives).
* Compare extractor matches `difference between X and Y` /
  `X vs Y` / `compare X and Y` (4 happy paths; 4 negatives).
* `allowed_url` accepts catalog hosts and rejects others.
* `filter_recommendations` drops any rec whose URL isn't in the allowlist.

### `tests/test_gemini_client.py` — Gemini REST adapter

`respx`-mocked HTTP. Validates:
* JSON-mode round trip and request-shape mapping (`system` → `systemInstruction`,
  `assistant` → `model`).
* Empty-candidates response raises `LLMError`.
* 429 retried (3 attempts) on transient throttle; 4xx (other than 429)
  raises immediately.
* Code-fence stripping (`\`\`\`json\n…\n\`\`\``) before JSON parse.
* **Daily-quota 429** falls through to the next model in the chain.
* **Per-minute 429** retries on the same model and does not switch.
* Empty key raises before any HTTP call.

### `tests/test_llm_factory.py` — provider selection

The factory wires up the Gemini backend by default. Tests cover:

* Default selection picks the Gemini client when `GEMINI_API_KEY` is set.
* Unknown provider names raise `ValueError`.
* The factory is the single touch-point for swapping backends — no
  consumer code references a concrete client class.

### `tests/test_cache.py` — LRU

* Basic put/get + miss returns None.
* LRU eviction at capacity.
* Touching a key (`.get`) protects it from eviction.
* Hit/miss counters update; `clear()` resets them.
* Capacity ≤ 0 raises ValueError.

### `tests/test_rate_limit.py` — token bucket

* Burst capacity allows N immediate consumes; the (N+1)th blocks.
* Per-key isolation: ip1 limit doesn't affect ip2.
* Tokens refill over wall-clock time.
* `evict_idle` purges old buckets.
* Middleware: returns 429 with the spec-shaped JSON body and a `Retry-After`
  header. Skips probe paths.

### `tests/test_metrics.py` — exposition format

* Counters with labels render with `{intent="search"} 2` lines.
* Naked counters (no labels) render without braces.
* Histograms emit `_bucket{le="..."}`, `_sum`, `_count`. Cumulative buckets
  count correctly.
* Label-value escaping handles `"`, `\`, newline.

### `tests/test_observability.py` — JSON logs + middleware

* `JsonFormatter` produces the canonical envelope (`ts`, `level`, `logger`,
  `event`, `request_id`).
* `extra={"attrs": {...}}` flows through to the rendered JSON.
* `configure_logging` is idempotent (no duplicate handlers).
* `RequestIdMiddleware` assigns when missing, echoes when supplied, and
  emits a structured access log line per request.
* `SecurityHeadersMiddleware` applies the four headers.

## How to run

```bash
pytest                          # full suite (~12 s after the ADR-0010 fastembed swap)
pytest -q                       # quiet
pytest tests/test_api.py        # one file
pytest -k injection             # name filter
pytest --cov=src --cov-report=term  # with coverage
pytest -x                       # stop at first failure
```

CI runs the suite on Python 3.11 and 3.12, plus `ruff` and `mypy`.

## Adding a regression test

When a real-world bug surfaces, write the test **first**, then fix:

1. Identify the layer (API / orchestrator / retrieval / guardrail).
2. Add a test in the matching file.
3. Run it — confirm it fails.
4. Fix the code; run again — confirm it passes.
5. Run the full suite to catch regressions.

## Live verification beyond pytest

Two harnesses for end-to-end checks against a running service (not run
in CI because they need a real API key and burn quota):

* **`scripts/eval.py --probes-only`** — 7 binary behavior probes
  (clarify on vague, refuse off-topic, recommend Java, resist
  injection, refine to personality, grounded compare, refuse legal).
  Mirrors SHL's own evaluator probes. Scoring goal: 7/7.
* **`scripts/eval.py --traces traces/`** — Recall@10 across labeled
  trace files (drop SHL's official traces here for the real number).
* **`scripts/verify_capabilities.py`** — 5 deeper scenarios that test
  the full SHL spec (non-linear input, JD-blob, 8-turn drip, grounded
  compare, resilience). More thorough than the binary probes.

Run them after any prompt change or fallback-path change.

## What the tests do **not** cover

* Performance / load — see `docs/operations.md` for capacity numbers; we
  don't bench in CI.
* The catalog scraper — it hits a live site (shl.com) and the HTML can drift.
  Run `python scripts/probe_retrieval.py` after a re-scrape to spot-check
  results.
* Real Recall@10 against SHL's holdout traces — only the labeled
  public traces (we don't have those checked in; drop them in `traces/`
  and run the eval).
