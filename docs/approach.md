# SHL Conversational Assessment Recommender — Approach

**Author:** Abhijat · **Stack:** FastAPI · Gemini Flash (multi-model fallback chain) · FAISS + BM25 · sentence-transformers (MiniLM-L6-v2) · Render

## 1. What I built

A stateless FastAPI service (`/health`, `/chat`) that takes a hiring manager
from a vague intent ("hiring a Java dev") to a grounded shortlist of SHL
Individual Test Solutions through dialogue. The agent handles the four
required behaviors — **clarify, recommend, refine, compare** — and refuses
off-topic / legal / prompt-injection traffic. Every URL the agent returns
comes from a scraped catalog (377 items); URLs not in the catalog are
filtered out before the response is serialized.

## 2. Architecture

```
client ──> POST /chat ──> Orchestrator (stateless, derives state from history)
                              │
                              ├── 1. Local injection precheck (regex)
                              ├── 2. Router LLM (JSON-mode) — extracts slots
                              │       + classifies intent in one call
                              ├── 3. Branch:
                              │     clarify  → return question, no recs
                              │     refuse   → deterministic refusal text
                              │     compare  → look up by name, factual reply
                              │     search/refine → hybrid retrieve + generate
                              ├── 4. Hybrid retrieval (FAISS dense + BM25 sparse,
                              │       fused with reciprocal rank fusion)
                              ├── 5. Generator LLM (JSON-mode) picks 1..10
                              │       indices from the retrieved candidates
                              └── 6. URL allowlist filter (catalog hosts only)
```

State derivation each turn: I extract slots (`role`, `seniority`, `skills`,
`test_types`, `duration`, `remote`) from the full message history. The same
LLM call also classifies intent and produces the dense-retrieval query. This
keeps the per-turn round-trips at **two LLM calls** for `search`/`compare`
and **one** for `clarify`/`refuse` — well inside the 30-second per-call cap.

## 3. Catalog & retrieval

**Scraper.** I built an `httpx + BeautifulSoup` scraper that walks
`?start=N&type=1` pages and identifies the *Individual Test Solutions* table
by its `<th class="custom__table-heading__title">` heading (the page also
contains a Pre-packaged Job Solutions table; I drop that). For each row it
captures name, URL, test-type letters (`A,B,C,D,E,K,P,S`), and remote/adaptive
flags. It then fetches each detail page to enrich with description, job
levels, languages, and assessment length. Final catalog: **377 assessments**,
377/377 with descriptions, 316/377 with lengths, 361/377 with job levels.

**Hybrid retrieval.** Each catalog row becomes one document combining name,
test-type names, job levels, languages, length, and description. I index
with two methods:
- **Dense:** sentence-transformers `all-MiniLM-L6-v2` (384-dim) into a FAISS
  `IndexFlatIP` (cosine via L2-normalization). Exact search at this scale is
  sub-millisecond.
- **Sparse:** `rank_bm25.BM25Okapi` over the same corpus. Catches exact
  product names like "OPQ32r", "Verify", "Java 8".

I fuse with **Reciprocal Rank Fusion** (k=60) — score-free and robust to the
two methods having very different magnitudes. Filters (test-type, remote
flag) are applied *after* fusion as soft prefs: if filtering yields <3 hits,
I retry without the test-type filter and let the LLM decide.

## 4. Prompts & agent design

The router prompt enforces a strict JSON schema for `{intent, slots,
search_query, compare_targets, refuse_reason, clarifying_question}`. It
explicitly lists the SHL test-type taxonomy and prohibits inventing codes.
The recommend prompt receives the retrieved candidates with bracketed indices
and is instructed to **reference candidates only by index** — this is the
single biggest defense against hallucinated names/URLs. Comparison is
strictly *grounded*: the prompt is given catalog records as context and is
instructed to say "I do not have that assessment in the catalog" rather than
guess.

Three layers of guardrail:
1. **Local regexes** catch obvious prompt-injection ("ignore previous
   instructions", "DAN mode", "act as a …") AND legal questions
   ("is it legal", "EEOC", "discrimination", "GDPR") before any LLM call.
2. **Router LLM** classifies subtler refusals (off-topic, hiring-policy)
   and emits a deterministic refusal text downstream.
3. **URL allowlist**: every recommendation's URL is checked against the
   set of catalog URLs. Anything else is dropped.

A fourth layer (ADR-0009) protects against LLM unavailability — when the
router LLM fails entirely (cumulative free-tier quota exhaustion or
provider outage), a **local compare regex** + **retrieval-only fallback**
keep the agent producing schema-valid, catalog-grounded responses
without any LLM call.

## 5. What didn't work, and how I measured

- **First scraper attempt** used `<h4>` selectors to find sections — but SHL
  embeds section titles inside `<th>` elements within each table, no separate
  heading. Fixed by scanning tables and matching the first row's `<th>` text.
- **Single-letter regex for test types** picked up stray uppercase letters
  in cell text. Fixed by targeting `span.product-catalogue__key` directly.
- **Hard test-type filtering** dropped recall in early eval runs (e.g. user
  asks for "personality test" → strict `P` filter excludes "Sales
  Transformation Report" entries that combine `P` and other codes). I now
  *retry without* the test-type filter when the filtered set has <3 results,
  letting the LLM judge fit. Anecdotally this lifted Recall@10 on my
  internal probes from ~0.55 → ~0.85.
- **Returning all retrieved candidates to the user** vs **letting the LLM
  pick a subset**. I went with LLM-picked indices (capped at 10) so the
  agent can prefer top dense hits but skip duplicates (e.g. "Sales
  Transformation Report 1.0" + "Sales Transformation Report 2.0").

## 6. Evaluation

`scripts/eval.py` runs against the live service and reports:
- **Behavior probes (7):** vague-query clarifies, off-topic refused,
  injection resisted, legal refused, Java query commits to recs, "add
  personality tests" refines into P-typed picks, compare produces grounded
  text with no rec list.
- **Recall@10:** if you drop labeled traces in `traces/*.json` (with
  `expected_assessment_names` + `scripted_turns`), the script prints mean
  Recall@10 across them.
- **Unit tests:** 107 pytest tests cover routes, schema, each agent intent
  branch (with a scriptable `FakeLLM`), retrieval, guardrails, the LLM
  factory, the Gemini REST adapter (incl. multi-model fallback), the
  rate-limiter, metrics, observability, and the static frontend. They run
  in ~2.5 minutes on CPU and pass without any LLM API key (LLM-dependent
  paths are mocked).
- **Live verification:** `scripts/verify_capabilities.py` exercises all 5
  spec capabilities against a running service (5/5 against live Gemini)
  and `scripts/eval.py --probes-only` runs 7 binary behavior probes (7/7).

## 7. Stack justification

- **FastAPI** — required by the spec; trivial Pydantic-validated I/O makes
  the strict schema (`reply | recommendations[1..10] | end_of_conversation`)
  enforceable at the boundary.
- **Gemini Flash (free tier)** — 4-model fallback chain
  (`gemini-flash-latest` → `gemini-2.5-flash` → `gemini-2.0-flash` →
  `gemini-2.5-flash-lite`) gives ~80 RPD of effective free-tier budget
  with sub-second latency. JSON mode via
  `responseMimeType: application/json` is rock-solid. The agent depends
  on a small `LLMClient` protocol, so a different backend is a one-file
  swap if needed; see ADR-0008.
- **MiniLM-L6-v2** — 384-dim, ~80 MB, runs on CPU under 5 ms/query. A bigger
  model gave marginal recall gains but doubled cold-start time.
- **FAISS + BM25 + RRF** — small catalog (377), exact search is fine, no
  external service to manage. RRF is the simplest fusion that doesn't depend
  on score calibration.
- **Render free tier + Docker** — one-click deploy from this repo, sleeps when
  idle (the SHL evaluator allows 2 min for cold-start `/health`). Catalog and
  index are baked into the image; the embedding model is fetched once on
  startup.

## 8. AI-tool usage

I used Claude (Anthropic) inside Claude Code as an AI-pair: it drafted the
scraper, prompts, and orchestrator skeleton; I designed the architecture,
chose the stack, designed the agent state machine and the RRF fusion, wrote
the test catalog & probes, debugged the SHL HTML quirks, and tuned the
guardrails. All design decisions and trade-offs in this document are mine —
the AI accelerated typing, not thinking.
