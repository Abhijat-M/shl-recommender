# Architecture

## 1. Goal

Take a hiring manager from a vague intent ("hiring a Java dev") to a grounded
shortlist of SHL Individual Test Solutions through dialogue. Every URL we
return must come from the SHL catalog; we never fabricate an assessment.

The service is **stateless**: every `POST /chat` carries the full conversation
history. The agent derives its working state (slots, intent) per turn.

## 2. System Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              Client                                      │
│   (curl / Postman / SHL replay harness / browser)                        │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │ HTTPS
┌────────────────────────────────▼─────────────────────────────────────────┐
│                          FastAPI service                                 │
│                                                                          │
│   ┌────────────────────────────────────────────────────────────────┐    │
│   │  Middleware chain                                              │    │
│   │   [SecurityHeaders]                                            │    │
│   │   [RequestId + structured access log]                          │    │
│   │   [RateLimit (token bucket / IP)]                              │    │
│   │   [CORS]                                                       │    │
│   └────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  │
│   │ /health     │  │ /ready      │  │ /version    │  │ /metrics     │  │
│   └─────────────┘  └─────────────┘  └─────────────┘  └──────────────┘  │
│                                                                          │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │                          POST /chat                              │  │
│   │                                                                  │  │
│   │      ┌─────────────────────────────────────────────────────┐    │  │
│   │      │                  Orchestrator                       │    │  │
│   │      │                                                     │    │  │
│   │      │  1. Local injection regex (precheck)                │    │  │
│   │      │  2. Router LLM (JSON) -> intent + slots + query     │    │  │
│   │      │  3. Branch:                                         │    │  │
│   │      │     clarify  -> clarifying question, no recs        │    │  │
│   │      │     refuse   -> deterministic refusal text          │    │  │
│   │      │     compare  -> lookup + grounded prose             │    │  │
│   │      │     search/refine -> hybrid retrieve + generator    │    │  │
│   │      │  4. Generator LLM (JSON) -> picks indices 1..10     │    │  │
│   │      │  5. URL allowlist filter                            │    │  │
│   │      └─────────────────────────────────────────────────────┘    │  │
│   │                                                                  │  │
│   │      ┌──────────────────────┐    ┌────────────────────────┐     │  │
│   │      │  HybridRetriever     │    │   GeminiClient         │     │  │
│   │      │   FAISS dense        │    │   - retry/backoff      │     │  │
│   │      │   BM25 sparse        │    │   - timeouts           │     │  │
│   │      │   RRF fusion         │    │   - JSON mode          │     │  │
│   │      │   LRU query cache    │    └────────────────────────┘     │  │
│   │      └──────────────────────┘                                    │  │
│   └──────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                          │                                   │
                          │                                   │
                          ▼                                   ▼
                ┌─────────────────────┐              ┌─────────────────┐
                │  data/catalog/      │              │   Gemini API    │
                │  catalog.json       │              │  (Flash family) │
                │  data/index/        │              └─────────────────┘
                │  *.faiss, bm25.pkl  │
                └─────────────────────┘
                          ▲
                          │  one-time, offline
                          │
                ┌─────────┴───────────┐
                │  scripts/           │
                │  scrape_catalog.py  │  -> shl.com (paginated)
                │  build_index.py     │
                └─────────────────────┘
```

## 3. Bounded Contexts (DDD)

| Bounded context | Module      | Responsibility                                |
|-----------------|-------------|-----------------------------------------------|
| Catalog         | `retrieval/catalog.py`  | Domain model (`Assessment`), persistence  |
| Retrieval       | `retrieval/retriever.py` + `indexer.py` + `cache.py` | Hybrid search + caching |
| Agent           | `agent/orchestrator.py` + `prompts.py` + `guardrails.py` | Intent routing, slot extraction, generation |
| LLM             | `llm/{base,gemini_client,factory}.py` | Gemini REST adapter behind a small `LLMClient` protocol |
| API             | `api/routes.py` + `schemas.py` | HTTP boundary, schema enforcement      |
| Web             | `web/index.html`        | Single-page chat UI mounted at `/`             |
| Cross-cutting   | `observability.py`, `rate_limit.py`, `metrics.py`, `config.py` | Middleware, settings  |

Modules talk through narrow interfaces. The agent never imports FastAPI; the
API never imports FAISS. This keeps each piece testable in isolation.

## 4. Data Flow: One `POST /chat` Turn

1. **Middleware**
   * Security headers added.
   * `X-Request-Id` assigned (echoes incoming if present).
   * Rate-limit token consumed (per client IP, bucket: 10 burst / 1 rps).
   * CORS handled.

2. **Route** (`api/routes.py::chat`)
   * Validate body against `ChatRequest` (Pydantic). 422 if malformed.
   * Trim history to `MAX_TURNS=8` (latest window) to honor the spec cap.

3. **Orchestrator pre-checks** (`agent/orchestrator.py::run`)
   * `is_obviously_injection(latest_user)` → return injection refusal,
     **no LLM call**.
   * `looks_legal(latest_user)` → return legal refusal, **no LLM call**.

4. **Router LLM** — `_route()` calls Gemini in JSON mode with
   `ROUTER_SYSTEM` + the serialized history. The Gemini client rolls
   through its model fallback chain on daily-quota 429s. Returns a `_RouterDecision` with `intent`, `slots`,
   `search_query`, `compare_targets`, `refuse_reason`,
   `clarifying_question`.

5. **Branch on success** (LLM router returned a decision):
   - **clarify** — return `decision.clarifying_question`, no recs.
     A clarify-loop guard forces a search after 2+ clarifies.
   - **refuse** — return `REFUSAL_REPLIES[reason]`, no recs.
   - **compare** — `lookup_by_names(targets)` + `COMPARE_SYSTEM` prompt
     with catalog records as the only context.
   - **search / refine** — see step 6.

6. **Graceful degradation** (LLM router raised `LLMError` — see ADR-0009):
   - **Compare-intent regex** on the latest user turn — if matched,
     emit deterministic compare summary from catalog data.
   - **Retrieval-only fallback** — concatenate all user messages, run
     hybrid retrieval, return top 3-5 with a generic reply. **No LLM
     call**.
   - **Clarify** — last resort if both above paths produce nothing.

7. **Hybrid Retrieval** (`retrieval/retriever.py::search`)
   * Cache key = `(query, filters, top_k)`. LRU-128. Hit returns cached list.
   * Dense: encode query with MiniLM via fastembed (ONNX Runtime, 384-dim,
     normalized), FAISS IndexFlatIP top-25.
   * Sparse: BM25Okapi top-25.
   * Fuse with Reciprocal Rank Fusion: `score = Σ 1/(60 + rank_i)`.
   * Apply soft filters (test types from slots; remote testing). If filters
     drop result-count < 3, retry without `test_types` filter.

8. **Generator** (`agent/orchestrator.py::_select`)
   * Send `RECOMMEND_SYSTEM` + history + slots + numbered candidate list.
   * LLM picks indices into the candidate list (NOT names).
   * Cap to `[1..10]`.

9. **URL allowlist** (`agent/guardrails.py`)
   * Drop any rec whose URL isn't in the catalog set. The schema layer caps
     to 10 items defensively.

10. **Response**
   * `ChatResponse` validated against the spec schema.
   * Metrics incremented (`shl_chat_requests_total`, `shl_chat_intent_total`,
     `shl_chat_recs_returned_total`, `shl_chat_duration_ms` histogram).
   * Structured access log emitted with `request_id`, `intent`, `n_recs`,
     `duration_ms`.

## 5. Catalog & Index Pipeline

```
shl.com /products/product-catalog/?start=N&type=1
         │
         ▼
   scrape_catalog.py
     - Identify "Individual Test Solutions" table by <th> header
     - Extract row: name, URL, test types, remote/adaptive flags
     - Hydrate detail page: description, job_levels, languages, length
         │
         ▼
   data/catalog/catalog.json   (377 entries committed to repo)
         │
         ▼
   build_index.py
     - Build retrieval doc per assessment (search_text())
     - Embed with MiniLM-L6-v2 via fastembed/ONNX (384-dim, L2-normalized)
     - FAISS IndexFlatIP
     - rank_bm25 BM25Okapi (alphanumeric tokenizer)
         │
         ▼
   data/index/   (dense.faiss + bm25.pkl + documents.pkl + meta.pkl)
```

The Docker image bakes both `catalog.json` and the built index, so cold start
is fast (no network calls to shl.com from the running service).

## 6. Configuration

All settings in `src/config.py` via `pydantic-settings`. Loaded from
environment variables; `.env` is read in dev. See `docs/operations.md` for
the full env-var matrix and operational defaults.

## 6.5. Resilience layers (LLM availability)

The agent has three independent paths to producing a useful response:

| Path | Trigger | LLM calls |
|------|---------|-----------|
| Local refuse (injection / legal) | Regex match on latest user message | 0 |
| Router + Generator (happy path) | LLM router succeeds | 1 (clarify/refuse/compare) or 2 (search/refine) |
| Local compare fallback | LLM router fails AND text matches compare regex | 0 |
| Retrieval-only fallback | LLM router fails AND not compare AND >= 2 words user input | 0 |
| Canned clarify | All above fail | 0 |

The agent therefore keeps producing schema-valid, catalog-grounded
responses even under total LLM outage. See ADR-0009 for the design
rationale and ADR-0008 for the multi-model fallback chain that runs
*inside* the LLM-call layer.

## 7. Performance Characteristics

| Operation                          | Latency (p50, CPU free tier) |
|------------------------------------|------------------------------|
| `/health`                          | < 5 ms                       |
| `/ready`                           | < 5 ms                       |
| FAISS search (377 docs)            | ~ 1 ms                       |
| BM25 search                        | ~ 5 ms                       |
| MiniLM encode (1 query, fastembed) | ~ 20 ms                      |
| LLM round trip (Gemini Flash Lite) | 400-700 ms                   |
| LLM round trip (Gemini 2.5 Flash)  | 700 ms - 1.5 s               |
| `/chat` (clarify)                  | 600 ms - 1.5 s (1 LLM call)  |
| `/chat` (search)                   | 1-3 s (2 LLM calls)          |
| `/chat` (retrieval-only fallback)  | < 100 ms (no LLM)            |

The 30-second per-call cap leaves substantial headroom even on cold start.

## 8. Why this architecture

Three forces shaped the design:

- **Spec rigidity.** The response schema is non-negotiable; we use Pydantic
  at the boundary so deviation is impossible.
- **Catalog grounding.** The agent must never invent a URL. The combination
  of "LLM picks indices, not names" + URL allowlist + index built only from
  scraped data means the agent has no way to surface a non-catalog item.
- **Cold-start sensitivity.** Render's free tier sleeps. The image bakes
  catalog, FAISS+BM25 index, AND the ONNX embedding weights — the
  runtime container starts with no network calls. Cold start is ~5-10 s.
- **Memory ceiling.** Render Free has a 512 MB RAM cap. fastembed
  (ONNX Runtime) keeps resident memory ~180-250 MB; sentence-transformers
  (PyTorch) crossed the cap at ~500-600 MB. See ADR-0010.
- **Provider availability.** Free-tier LLM quotas are tight (e.g., Gemini
  20 RPD per model). ADR-0008's multi-model chain plus ADR-0009's
  retrieval-only fallback keep the agent useful under total LLM outage.

See `docs/decisions/` for ADRs covering each major choice.
