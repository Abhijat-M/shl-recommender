# Changelog

All notable changes to this project are tracked here. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added (2026-05-09 deployment to Render)
- **Live deployment** at `https://shl-recommender-yxcn.onrender.com`
  on Render Free (512 MB RAM tier).
- **GitHub Actions CI workflow** (`.github/workflows/ci.yml`) — runs
  ruff + mypy + pytest on Python 3.11 / 3.12 plus a Docker build sanity
  check on every push and PR.
- **ADR-0010** documenting the fastembed migration and its rationale.

### Changed (deploy-driven)
- **Embeddings: `sentence-transformers` → `fastembed` (ONNX Runtime)** —
  same `all-MiniLM-L6-v2` weights (byte-identical), but loaded via
  ONNX Runtime instead of PyTorch. Resident memory dropped from
  ~500-600 MB to ~180-250 MB, fitting Render Free's 512 MB cap. Image
  ~600 MB smaller, pytest ~12 s instead of ~100-150 s.
- `requirements.txt`: dropped `sentence-transformers`, added
  `fastembed==0.8.0` + `onnxruntime==1.26.0`.
- `Dockerfile`: builder stage caches the ONNX weights at
  `/opt/fastembed_cache`; runtime stage `COPY`s them so first request
  needs no network.
- `src/retrieval/indexer.py` and `src/retrieval/retriever.py` pass
  `cache_dir` explicitly to `TextEmbedding(...)` from the
  `FASTEMBED_CACHE_DIR` env var — fastembed's own env var name is
  unstable across versions; passing the kwarg is foolproof.
- `render.yaml`: env vars renamed from `GROQ_API_KEY`/`GROQ_MODEL` to
  `GEMINI_API_KEY`/`GEMINI_MODEL`/`GEMINI_FALLBACK_MODELS` (initial
  blueprint was stale from the pluggability work).

### Added
- **`LLMClient` protocol + Gemini backend** (ADR-0008). New
  `GeminiClient` (REST over httpx; JSON mode via
  `responseMimeType: application/json`). The orchestrator depends only
  on the protocol so a different backend is a one-file swap.
- **Multi-model Gemini fallback chain** (ADR-0008 follow-up). New
  `GEMINI_FALLBACK_MODELS` env var. On a daily-quota 429 the client
  rolls forward to the next model; per-minute 429s still retry on the
  same model. Default chain:
  `gemini-flash-latest, gemini-2.5-flash, gemini-2.0-flash, gemini-2.5-flash-lite`
  (~80 RPD on free tier).
- **Graceful degradation when the LLM is unavailable** (ADR-0009):
  - `extract_compare_targets()` regex catches "difference between X and Y"
    / "X vs Y" / "compare X and Y" so compare requests work without LLM.
  - `_retrieval_only_fallback()` runs hybrid retrieval against the
    cumulative user text when the router LLM fails. Returns 3-5
    catalog-grounded recs instead of looping on canned clarify.
- **Local legal-question fast-path** — 10 regex patterns (`is it legal`,
  `EEOC`, `discrimination`, `GDPR`, `lawsuit`, etc.) catch the largest
  abuse class before any LLM call. 2 ms templated refusal.
- **Frontend** — minimal/modern single-page chat UI mounted at `/`.
  Light theme with off-white canvas and frosted-glass surfaces over an
  animated water-blur backdrop. Refined test-type chips, recommendation
  cards with catalog links, Inter typography. No build step; one HTML
  file. CSP loosened on the HTML route only; JSON endpoints keep
  `default-src 'none'`.
- `TransientLLMError` for retriable failures (timeouts, 429, 5xx);
  permanent 4xx and malformed JSON now short-circuit the retry budget.
- `/version` reports active provider as `provider:model`.
- `/ready` `llm_configured` accounts for either backend's key.
- New tests:
  - `test_gemini_client.py` (12) including daily-quota fallthrough +
    per-minute retry.
  - `test_llm_factory.py` (5).
  - `test_web.py` (4) — index served at `/` with correct CSP.
  - 14 new legal-detection cases in `test_guardrails.py`.
  - 8 new compare-extraction cases in `test_guardrails.py`.
  - 3 new orchestrator cases for the LLM-failure paths.

### Changed
- Default LLM provider is **Gemini Flash** (`gemini-flash-latest` primary,
  with 3-model fallback chain). Per-model free-tier limit is **20 RPD**;
  the chain expands effective budget to ~80 RPD.
- Router prompt: more decisive "decision rules" with examples; bias
  toward `search` once role + one qualifier is present.
- Router `max_tokens` 600 → 1500; recommend `max_tokens` 400 → 800
  (Gemini Flash writes longer JSON than Flash Lite — was getting truncated).
- Clarify-loop guard: forces `search` after 2+ clarifies in the same
  conversation.
- `.env.example` documents both providers; pick one.
- `src/llm/client.py` is now a backward-compat shim re-exporting from
  `src/llm/{base,gemini_client,factory}.py`.

### Verified live
- 7/7 behavior probes pass against live Gemini.
- 5/5 capability scenarios pass (`scripts/verify_capabilities.py`).
- 107/107 pytest, ruff clean, mypy clean.

## [0.1.0] - 2026-05-09

Initial release. Built end-to-end for the SHL Labs AI Intern take-home.

### Added
- Stateless FastAPI service with `GET /health`, `GET /ready`,
  `GET /version`, `GET /metrics`, `POST /chat`.
- Conversational agent with five intents: clarify, search, refine, compare,
  refuse.
- Hybrid retrieval: FAISS dense (MiniLM-L6-v2) + BM25 sparse + RRF fusion.
- LRU query cache (capacity 128, process-local).
- Defense-in-depth guardrails: local injection regex, LLM router classifier,
  URL allowlist.
- Catalog scraper that handles SHL pagination and detail-page hydration.
  Output: 377 Individual Test Solutions in `data/catalog/catalog.json`.
- Production middleware: structured JSON logs, request IDs (X-Request-Id
  echo + access logging), security headers, in-memory token-bucket rate
  limiter (10 burst / 1 rps per client IP), CORS.
- Prometheus-format `/metrics` (in-process registry; counters + histograms).
- 59 pytest tests covering API, orchestrator, retrieval, guardrails, cache,
  rate limit, metrics, observability. Runs without a Gemini API key.
- Behavior-probe + Recall@10 evaluation script (`scripts/eval.py`).
- Multi-stage Dockerfile that bakes catalog + index into the image.
- Render blueprint (`render.yaml`) for one-click deploy.
- GitHub Actions CI: ruff + mypy + pytest on Python 3.11/3.12 + Docker build.
- Pre-commit hooks (ruff, ruff-format, mypy, whitespace, secret detection).
- Documentation suite: architecture, API reference, operations runbook,
  development guide, testing strategy, security threat model, 9 ADRs,
  agent-prompt reference, approach document.

### Configuration
- 17 environment variables documented in [docs/operations.md](docs/operations.md).
- Strict response schema enforced via Pydantic (non-negotiable per spec).
- Spec-honored limits: 8-turn cap, 30 s per call (LLM internal: 20 s),
  1-10 recommendations.

<!-- Once the repo is on GitHub, replace YOUR_ORG below with the owner.
[Unreleased]: https://github.com/YOUR_ORG/shl-recommender/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YOUR_ORG/shl-recommender/releases/tag/v0.1.0
-->
