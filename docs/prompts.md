# Agent Prompts

This is the canonical reference for every prompt the agent uses, the
JSON contract each one expects, and what the orchestrator does with the
output. The actual source lives in
[src/agent/prompts.py](../src/agent/prompts.py).

## Prompt overview

| Name | When called | Output shape | Max tokens |
|------|-------------|--------------|-----------:|
| `ROUTER_SYSTEM` | every turn (after local refuse fast-paths) | `_RouterDecision` JSON | 1500 |
| `RECOMMEND_SYSTEM` | when intent ∈ {search, refine} | `{reply, selected_indices, end_of_conversation}` | 800 |
| `COMPARE_SYSTEM` | when intent == compare | `{reply, end_of_conversation}` | 800 |

All three are sent in **JSON mode** so the response is guaranteed to be
parseable JSON. We still validate at the call site (`json.loads`) and
fall through to deterministic text on parse failure.

## 1. Router prompt — `ROUTER_SYSTEM`

**Purpose.** One LLM call that does three jobs at once: classifies the
intent, extracts structured slots from the conversation history, and
generates a natural-language search query the retriever will use.

**JSON contract**

```json
{
  "intent": "clarify" | "search" | "refine" | "compare" | "refuse",
  "slots": {
    "role": string|null,
    "seniority": "intern"|"junior"|"mid"|"senior"|"lead"|"executive"|null,
    "skills": string[],
    "test_types": string[],            // SHL letter codes A,B,C,D,E,K,P,S
    "duration_minutes_max": number|null,
    "remote_testing": boolean|null,
    "languages": string[]
  },
  "search_query": string,              // always present, dense NL query
  "compare_targets": string[],         // only when intent="compare"
  "refuse_reason": "off_topic"|"injection"|"legal"|"general_hiring"|null,
  "clarifying_question": string,       // only when intent="clarify"
  "rationale": string                  // <= 1 sentence; for logs
}
```

**Decision rules** (the prompt enumerates these in order):
1. Refuse on injection / off-topic / legal / general-hiring.
2. Compare on explicit "X vs Y" / "difference between X and Y".
3. Refine when the user changes a constraint over a prior shortlist.
4. Search when role + one qualifier is present (default bias).
5. Clarify only on truly vague seeds. Hard cap: max 2 clarifies in a
   conversation (orchestrator-level guard also enforces this).

**Why one call instead of separate "classify" + "extract" calls.** It
keeps latency under 1 s and removes the risk of inconsistent state
between two calls (e.g. classify says "search" but extract says no role).

## 2. Recommend prompt — `RECOMMEND_SYSTEM`

**Purpose.** Pick a 1-10 subset from the retrieved candidates that best
fits the slots. The candidates are passed in a numbered list:

```
[0] Java 8 (New) (types=K; ~25 min) :: Multi-choice test that…
[1] Core Java (Advanced Level) (New) (types=K; ~30 min) :: …
…
```

**JSON contract**

```json
{
  "reply": string,                  // <= 3 sentences; do NOT enumerate names
  "selected_indices": number[],     // 1..10 integers into the candidate list
  "end_of_conversation": boolean
}
```

**Why indices, not names.** This is the load-bearing anti-hallucination
defense (ADR-0003). The model can't invent a non-catalog assessment if
its only output is integers. Names and URLs are reconstructed from the
catalog at those indices.

**Why the model reply does NOT enumerate names.** The structural
`recommendations[]` field in the API response is the source of truth.
Duplicating names in `reply` invites hallucinated drift; the prompt
explicitly forbids it.

## 3. Compare prompt — `COMPARE_SYSTEM`

**Purpose.** Produce a grounded factual comparison from catalog records.

**Inputs.** The orchestrator looks up the named targets via
`HybridRetriever.lookup_by_names(fuzzy=True)` and serializes the matched
records into the prompt context. The model is instructed to:

- Use ONLY the supplied catalog data.
- Say "I do not have that assessment in the catalog" if a target was
  not matched.
- Keep the reply to 3-5 sentences, plain prose.

**JSON contract**

```json
{
  "reply": string,
  "end_of_conversation": false
}
```

The `recommendations` field is empty for compare turns by design — the
agent is producing prose, not a shortlist.

## 4. Refusal templates — `REFUSAL_REPLIES`

Four deterministic strings used when the router classifies `intent=refuse`
or when a local regex pre-filter matches:

| Reason key | Trigger |
|---|---|
| `injection` | Local injection regex OR router `refuse_reason="injection"` |
| `legal` | Local legal regex OR router `refuse_reason="legal"` |
| `off_topic` | Router `refuse_reason="off_topic"` |
| `general_hiring` | Router `refuse_reason="general_hiring"` |

We do NOT let the LLM compose refusal text. Templates are stable, on-brand,
auditable, and they cannot be steered by a malicious prompt.

## Token budget

Worst-case per-turn (cumulative; ~4k tokens):

| Call | Input | Output cap |
|------|------:|-----------:|
| Router | ~1,560 | 1,500 |
| Recommend (when search/refine) | ~1,550 | 800 |
| Compare (when compare) | ~640 | 800 |

These are well under Gemini's per-call context window (1M tokens).
The per-minute risk is real on free tier — see
[ADR-0008](decisions/0008-pluggable-llm-provider.md) for the multi-model
fallback chain.

## Iterating on prompts

1. Edit `src/agent/prompts.py`.
2. Run unit tests: `pytest tests/test_orchestrator.py -q`. These use
   `FakeLLM` so they don't burn real-LLM quota.
3. Run live behavior probes: `python scripts/eval.py --probes-only`.
   Goal: 7/7.
4. Run capability verification: `python scripts/verify_capabilities.py`.
   Goal: 5/5.
5. If recall regresses, run the retrieval probe:
   `python scripts/probe_retrieval.py` to see if it's a retrieval
   issue vs a prompt issue.

## Why prompts are not feature-flagged per provider

We tested both `gemini-2.5-flash-lite` and `gemini-flash-latest` and
`llama-3.3-70b-versatile` against the same prompts. All three score 7/7
on probes. Until we see a divergence that warrants per-provider prompts,
we keep one set — simpler to maintain.
