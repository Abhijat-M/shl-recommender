# Contributing

Thanks for opening this project. The goal of this guide is to make the
first PR easy to land.

## Getting set up

See [docs/development.md](docs/development.md) for a full local setup
guide. The short version:

```bash
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env  # set GEMINI_API_KEY
python scripts/build_index.py
pytest -q
```

## Pre-commit

```bash
pip install pre-commit
pre-commit install
```

Every commit will then auto-run:
- `ruff check --fix`
- `ruff format`
- `mypy src/`
- whitespace / EOL / large-file / private-key checks

## What lands easily

- Bug fixes with a regression test.
- Doc-only changes (typos, clarifications, new ADRs).
- New behavior probes in `scripts/eval.py` with documented rationale.
- Catalog refreshes — re-run the scraper, commit `data/catalog/catalog.json`,
  open a PR. Diff makes it reviewable.
- Performance improvements with a measurement (before/after p50/p95).

## What needs discussion first

Open an issue or draft PR before doing any of:
- Changing the response schema (any field on `ChatResponse`).
- Changing the agent's intent set or refusal categories.
- Adding a new LLM provider or swapping the embedding model.
- Adding a runtime dependency.
- Changes that loosen the URL allowlist or guardrails.

All of these need a corresponding ADR in `docs/decisions/`.

## Coding style

- Files capped around 500 lines (soft).
- No comments that explain *what* — only *why*.
- One responsibility per module; modules talk through narrow interfaces.
- Tests use the fixtures in `tests/conftest.py` (`fake_llm`, `retriever`,
  `orchestrator`).

## PR checklist

- [ ] `pytest -q` green.
- [ ] `ruff check src tests scripts` green.
- [ ] `mypy src` green.
- [ ] Updated `CHANGELOG.md` under `## Unreleased`.
- [ ] Updated docs if behavior changed.
- [ ] No `.env`, secrets, or large binaries committed.

## Reporting a security issue

See [docs/security.md](docs/security.md). Don't open public issues for
vulnerabilities.
