# SHL Conversational Assessment Recommender

A grounded conversational agent that recommends SHL Individual Test Solutions
through dialogue. Built for the SHL Labs AI Intern take-home.

> **Stateless** · **Schema-strict** · **Catalog-grounded** · **Production-ready**

Python 3.11+ · MIT licensed · 107 passing tests · 377-item SHL catalog

---

## Table of Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Architecture at a glance](#architecture-at-a-glance)
- [API endpoints](#api-endpoints)
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Tests + evaluation](#tests--evaluation)
- [Deployment](#deployment)
- [License](#license)

---

## What it does

Takes a hiring manager from a vague intent ("hiring a Java dev") to a
grounded shortlist of SHL assessments via dialogue. Handles five intents:

| Intent     | When                                              | Output             |
|------------|---------------------------------------------------|--------------------|
| **Clarify** | Vague query, missing role/seniority              | Question, no recs  |
| **Recommend** | Enough context — commits to a shortlist         | 1-10 recs          |
| **Refine** | Mid-conversation constraint change ("add personality") | Updated 1-10 recs |
| **Compare** | "What's the difference between OPQ32r and Verify?" | Grounded prose, no recs |
| **Refuse** | Off-topic, legal, prompt-injection                | Templated refusal  |

Every URL it returns comes from the [scraped SHL catalog](data/catalog/catalog.json)
— it cannot fabricate one (see [ADR 0003](docs/decisions/0003-index-not-name.md)).

## Quick start

```powershell
# Windows PowerShell (use bash equivalents on Unix)
git clone <repo> && cd shl-recommender
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env
# edit .env — set GEMINI_API_KEY (free at https://aistudio.google.com/app/apikey)

python scripts/build_index.py    # ~30 s; catalog.json is committed
uvicorn src.main:app --port 8000

# Smoke test
curl http://localhost:8000/health
curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hiring a mid-level Java dev"}]}'
```

Full setup: [docs/development.md](docs/development.md).

## Architecture at a glance

```
client ──> POST /chat ──> Orchestrator (stateless)
                              │
                              ├── 1. Local injection regex precheck
                              ├── 2. Router LLM (JSON) — slots + intent
                              ├── 3. Branch by intent
                              ├── 4. Hybrid retriever (FAISS + BM25 + RRF)
                              ├── 5. Generator LLM picks indices
                              └── 6. URL allowlist filter
```

Stack:
- **FastAPI** + Pydantic (schema-strict)
- **Gemini Flash** (free tier, 20 RPD × 4-model fallback chain) — wired behind a small `LLMClient` protocol so a different backend is a one-file swap if needed
- **sentence-transformers** MiniLM-L6-v2 (384-dim) → **FAISS** IndexFlatIP
- **rank_bm25** Okapi → **Reciprocal Rank Fusion**
- In-process: structured JSON logs, request IDs, token-bucket rate
  limiter, Prometheus `/metrics`, security headers
- **Render** Free tier, multi-stage Dockerfile

Full design: [docs/architecture.md](docs/architecture.md).

## API endpoints

| Method | Path        | Purpose                              |
|--------|-------------|--------------------------------------|
| GET    | `/`         | **Chat UI** (single-page, vanilla JS) |
| GET    | `/health`   | Liveness                             |
| GET    | `/ready`    | Readiness (index loaded + LLM ready) |
| GET    | `/version`  | Build metadata                       |
| GET    | `/metrics`  | Prometheus text format               |
| POST   | `/chat`     | Conversational turn (stateless)      |

- **Chat UI** at `/` — single-page, light theme with off-white canvas
  and frosted-glass surfaces over an animated water-blur backdrop.
  Recommendation cards have color-coded test-type chips with grounded
  catalog links. No build step; the page is a single static file.
- **OpenAPI** at `/docs` (Swagger UI). Full reference: [docs/api.md](docs/api.md).

## Project layout

```
shl-recommender/
├── src/
│   ├── api/             # FastAPI routes + Pydantic schemas
│   ├── agent/           # Orchestrator, prompts, guardrails
│   ├── llm/             # Gemini client (retry, timeout, multi-model fallback)
│   ├── retrieval/       # Catalog model, indexer, retriever, cache
│   ├── observability.py # JSON logs, request IDs, security headers
│   ├── rate_limit.py    # Token bucket
│   ├── metrics.py       # Prometheus-format metrics
│   ├── config.py        # Pydantic settings
│   └── main.py          # App factory + lifespan
├── tests/               # 107 tests (pytest, runs without a real LLM)
├── scripts/             # scrape_catalog, build_index, eval, verify_catalog
├── data/
│   ├── catalog/         # catalog.json (377 entries, committed)
│   └── index/           # FAISS + BM25 (gitignored, built locally)
├── docs/
│   ├── architecture.md  # System design with diagrams
│   ├── api.md           # API reference
│   ├── operations.md    # Runbook
│   ├── development.md   # Local dev guide
│   ├── testing.md       # Test strategy
│   ├── security.md      # Threat model
│   ├── approach.md      # 2-page approach (submission)
│   └── decisions/       # 9 ADRs
├── .github/workflows/   # CI (ruff + mypy + pytest + Docker)
├── Dockerfile           # Multi-stage, bakes catalog + index
├── render.yaml          # Deploy blueprint
├── pyproject.toml       # ruff + mypy + pytest config
├── .pre-commit-config.yaml
└── requirements.txt
```

## Documentation

| Document                                    | Audience    | Purpose |
|---------------------------------------------|-------------|---------|
| [docs/architecture.md](docs/architecture.md) | Engineers   | System design, data flow, perf characteristics |
| [docs/api.md](docs/api.md)                   | API users   | Endpoints, schemas, examples |
| [docs/operations.md](docs/operations.md)     | On-call     | Deploy, env vars, incidents, rollback |
| [docs/development.md](docs/development.md)   | Contributors| Setup, common tasks, debugging |
| [docs/testing.md](docs/testing.md)           | Contributors| Test strategy, layers, fixtures |
| [docs/security.md](docs/security.md)         | Reviewers   | Threat model, mitigations |
| [docs/approach.md](docs/approach.md)         | SHL evaluator | 2-page approach summary |
| [docs/prompts.md](docs/prompts.md)           | Engineers   | Reference of every agent prompt + JSON contract |
| [docs/data.md](docs/data.md)                 | Engineers   | Catalog schema, SHL test-type taxonomy, scrape→index pipeline |
| [docs/frontend.md](docs/frontend.md)         | Engineers   | UI structure, theming tokens, JS lifecycle |
| [docs/decisions/](docs/decisions/)           | All         | 9 ADRs (one per major architectural choice) |
| [CONTRIBUTING.md](CONTRIBUTING.md)           | Contributors| PR checklist, what lands easily |
| [CHANGELOG.md](CHANGELOG.md)                 | All         | Release notes |

## Tests + evaluation

```bash
pytest                          # full suite — 107 tests, ~150 s
ruff check src tests scripts    # lint
mypy src                        # type-check

# Eval against the live service
python scripts/eval.py --base-url http://127.0.0.1:8000 --probes-only          # 7 binary probes
python scripts/eval.py --base-url http://127.0.0.1:8000 --traces traces/       # Recall@10
python scripts/verify_capabilities.py --base-url http://127.0.0.1:8000         # 5-scenario E2E
```

Strategy: [docs/testing.md](docs/testing.md).

## Deployment

**Render** (recommended for the SHL submission):
1. Push the repo to GitHub.
2. render.com → **New** → **Blueprint** → connect repo.
3. In the env vars panel, set `GEMINI_API_KEY` (the only required secret).
4. Deploy. The blueprint reads `render.yaml`; first build runs `scripts/build_index.py`.

**Docker** (any host):
```bash
docker build -t shl-recommender .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=$KEY shl-recommender
```

Full ops guide: [docs/operations.md](docs/operations.md).

## License

[MIT](LICENSE)
