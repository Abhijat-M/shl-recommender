# ADR 0004: Two-stage agent — router + generator

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The agent has to do four very different things per turn:
1. Decide whether to clarify, refuse, recommend, refine, or compare.
2. Extract structured slots (`role`, `seniority`, `skills`, `test_types`,
   `duration`, `remote_testing`, `languages`).
3. Generate a search query that matches the catalog's vocabulary.
4. Compose the natural-language reply.

Each of these has different prompt semantics. Cramming them into one prompt
makes the prompt huge, the output schema unwieldy, and tuning fragile.

## Decision

Two LLM calls per turn (for `search`/`refine` intents):

* **Stage 1 — Router** (`ROUTER_SYSTEM`). One JSON-mode call. Outputs:
  - `intent` ∈ {clarify, search, refine, compare, refuse}
  - `slots` (a flat dict)
  - `search_query` (a natural-language sentence)
  - `compare_targets` (when intent=compare)
  - `refuse_reason` (when intent=refuse)
  - `clarifying_question` (when intent=clarify)
  - `rationale` (one sentence; logged for debugging)

* **Stage 2 — Generator** (`RECOMMEND_SYSTEM`). One JSON-mode call.
  Outputs:
  - `reply` (≤ 3 sentences)
  - `selected_indices` (1-10 integers)
  - `end_of_conversation` (boolean)

For `clarify` and `refuse` intents, only the router runs. For `compare`,
the router + a separate compare generator (`COMPARE_SYSTEM`) runs.

## Alternatives considered

1. **Single LLM call producing everything.** Rejected: schema gets messy,
   and the model often skipped the search query when it decided to clarify,
   leaving us no fallback for retries.
2. **Tool-calling pattern (LLM decides which tool to invoke).** Rejected:
   tool-calling latency is higher than JSON-mode on Gemini, and the routing
   decisions in this domain are coarse enough that an explicit two-stage
   pipeline is more debuggable.
3. **Three-stage: router → reranker → generator.** Considered for Recall@10
   gains but rejected: the recall improvements weren't worth the latency
   budget. The generator already does an implicit rerank by picking indices.

## Consequences

**Positive:**
- Each prompt has one job; small, easy to iterate on, easy to test.
- The router's JSON output is a structured artifact the orchestrator can
  log, route on, and validate.
- We can cheaply degrade: if Stage 2 fails, we fall back to Stage 1's
  top-K candidates with a generic reply.

**Negative:**
- 2 round trips to Gemini for `search`. We measure ~1.5 s p50 — comfortably
  inside the 30 s spec cap.
- More prompt tokens overall. Within Gemini's free-tier 1M-TPM ceiling
  this is fine; for paid environments we'd consolidate where prompts are short.
