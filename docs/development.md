# Development Guide

## Prerequisites

- Python 3.11+ (3.12 also tested in CI)
- A free Gemini API key — https://aistudio.google.com/app/apikey
- ~2 GB free disk for the embedding model + dependencies

## First-time setup

```bash
git clone <repo>
cd shl-recommender

# 1. Create venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate    # macOS / Linux

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env: set GEMINI_API_KEY at minimum

# 4. Build the catalog + index (one-time, ~5 minutes total)
python scripts/scrape_catalog.py     # produces data/catalog/catalog.json
python scripts/build_index.py        # produces data/index/*

# 5. Run the service
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 6. Smoke test (in another shell)
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hiring a Java dev"}]}'
```

If `data/catalog/catalog.json` is committed already, you can skip step 4
and just run `python scripts/build_index.py`.

## Project layout

```
shl-recommender/
├── src/
│   ├── api/             # FastAPI routes + Pydantic schemas
│   ├── agent/           # Orchestrator, prompts, guardrails
│   ├── llm/             # Gemini client (retry, timeout, multi-model fallback)
│   ├── retrieval/       # Catalog model, indexer, retriever, cache
│   ├── config.py        # Settings (pydantic-settings)
│   ├── observability.py # JSON logs, request IDs, security headers
│   ├── rate_limit.py    # Token bucket
│   ├── metrics.py       # Prometheus-format metrics
│   └── main.py          # App factory + lifespan
├── tests/               # 107 tests, all green
├── scripts/             # scrape_catalog, build_index, eval, verify_catalog
├── data/
│   ├── catalog/         # catalog.json (committed)
│   └── index/           # FAISS + BM25 (gitignored, built locally)
├── docs/                # this directory
│   └── decisions/       # ADRs
├── .github/workflows/   # CI
├── Dockerfile           # multi-stage prod image
├── render.yaml          # deploy blueprint
├── pyproject.toml       # ruff, mypy, pytest configs
└── requirements.txt
```

## Common tasks

### Run the test suite

```bash
pytest -q                       # all tests, quiet
pytest tests/test_api.py -v     # one file, verbose
pytest -k injection             # name filter
pytest --cov=src                # with coverage
```

The first run takes ~100 s because the conftest builds a small in-memory
FAISS index. Subsequent runs reuse the session-scoped fixture.

### Lint + type-check

```bash
ruff check src tests scripts    # lint
ruff format src tests scripts   # auto-format
mypy src                        # type-check
```

CI runs all three on every push. Pre-commit (`pre-commit install`) runs
them on every commit locally.

### Test the agent against real conversation flows

```bash
# 1. Start the server
uvicorn src.main:app --port 8000

# 2a. Behavior probe suite (7 binary assertions, mirrors SHL spec scoring)
python scripts/eval.py --base-url http://127.0.0.1:8000 --probes-only

# 2b. End-to-end capability verification (5 scenarios, more thorough)
python scripts/verify_capabilities.py --base-url http://127.0.0.1:8000
```

`scripts/eval.py` runs 7 binary probes (clarify, refuse, recommend,
refine, compare, injection, legal). If you have labeled trace files,
drop them into `traces/` and call:

```bash
python scripts/eval.py --base-url http://127.0.0.1:8000 --traces traces/
```

`scripts/verify_capabilities.py` runs 5 scenarios that exercise the
deeper SHL spec capabilities — non-linear conversation, JD-blob on
turn 1, 8-turn context drip, grounded compare, and resilience to edge
inputs. Useful as a regression harness when you change prompts or the
fallback paths.

### Update the catalog

```bash
python scripts/scrape_catalog.py
python scripts/build_index.py
git add data/catalog/catalog.json
git commit -m "chore(catalog): refresh"
```

Note: `data/index/` is gitignored — it's regenerated on each build.

### Tune the agent / retrieval

* **Prompts** live in [src/agent/prompts.py](../src/agent/prompts.py). See
  [docs/prompts.md](prompts.md) for a full reference of what each prompt
  does and the JSON contracts they enforce.
* **Retrieval knobs** are env vars (`TOP_K_DENSE`, `TOP_K_SPARSE`,
  `TOP_K_FINAL`, `RRF_K`).
* **Hard filters** are in `agent/orchestrator.py::_handle_search` — adjust
  the soft-fallback logic if filters are dropping too aggressively.
* **Local-refuse regexes** are in
  [src/agent/guardrails.py](../src/agent/guardrails.py): `INJECTION_PATTERNS`,
  `LEGAL_PATTERNS`, `_COMPARE_PATTERNS`. Each match short-circuits the LLM.
* **LLM provider/model** is selected by `LLM_PROVIDER` env var. Multi-model
  fallback for Gemini is `GEMINI_FALLBACK_MODELS` (comma-separated). See
  ADR-0008 and ADR-0009 for the design.

### Add a new test

1. Add a file under `tests/`, e.g. `tests/test_my_feature.py`.
2. Use `conftest.py` fixtures (`retriever`, `orchestrator`, `fake_llm`).
3. For LLM-dependent paths, queue the expected JSON on `fake_llm.json_responses`.

```python
@pytest.mark.asyncio
async def test_my_thing(orchestrator, fake_llm):
    fake_llm.json_responses = [
        {"intent": "clarify", "slots": {}, "search_query": "x",
         "compare_targets": [], "refuse_reason": None,
         "clarifying_question": "?", "rationale": ""}
    ]
    result = await orchestrator.run([{"role": "user", "content": "..."}])
    assert result.intent == "clarify"
```

## Debugging tips

* **Server returns the fallback "I hit an internal issue" reply**: check the
  `chat handler failed` log line; it has the full traceback and request id.
* **All chats return clarify**: probably the LLM call is failing. Verify
  `GEMINI_API_KEY` and check `/version` to confirm the model name.
* **Recall@10 is low**: turn `LOG_LEVEL=DEBUG` and inspect the
  `_handle_search` log lines for which filters are being applied. Often the
  `test_types` filter is too narrow; the orchestrator already retries
  without it when results < 3, but for some queries you may want to
  override.

## Scripts overview

| Script | Purpose | When to run |
|--------|---------|-------------|
| `scripts/scrape_catalog.py` | Walk shl.com pagination, write `data/catalog/catalog.json` | When the SHL catalog changes (rare) |
| `scripts/build_index.py`    | Build FAISS dense + BM25 sparse from catalog.json | After a re-scrape; on first setup |
| `scripts/verify_catalog.py` | Print catalog stats and 5 sample entries | Sanity-check after a scrape |
| `scripts/probe_retrieval.py`| Smoke-test the retriever with 6 representative queries | When tuning retrieval |
| `scripts/probe_gemini_models.py` | List Gemini models the key can reach + test each | When debugging quota/404 errors |
| `scripts/probe_tokens.py`   | Measure worst-case input/output tokens per /chat | After prompt changes (token-budget audit) |
| `scripts/eval.py`           | Behavior-probe + Recall@10 harness (7 binary asserts) | After every behavior-affecting change |
| `scripts/verify_capabilities.py` | 5-scenario live capability check | Same; deeper than eval probes |

## Code style

* `ruff` config in `pyproject.toml`. Line length 100.
* `mypy` mode is "gradual" — strict on most modules; `Any` on the
  faiss/sentence_transformers boundary because their types are unstable.
* Files capped at ~500 lines (a soft guideline; the orchestrator is the
  largest at ~570).
* No comments that just explain *what*; only *why* the code is non-obvious.

## Releasing

Versioning is `src/__init__.py::__version__`. To cut a release:

1. Bump `__version__`.
2. Update `CHANGELOG.md`.
3. `git tag vX.Y.Z && git push --tags`.

There's no separate publish step — the live service deploys from `main`.
