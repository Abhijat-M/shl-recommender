# ADR 0003: LLM picks indices, not names (URL allowlist)

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The hardest failure mode in this category of system is the agent
**hallucinating a recommendation** — confidently returning an assessment
name and a URL that don't exist in the catalog. The SHL evaluator marks
hallucinated items as wrong; even one can collapse Recall@10.

Once the LLM emits a free-form name + URL, we have no perfect way to
verify it post-hoc against the catalog (fuzzy name matching is lossy).

## Decision

The recommend-stage prompt presents candidates in a numbered list:

```
Candidates (index, name, test_types):
[0] Java 8 (New) (types=K; ...) :: Multi-choice test ...
[1] Core Java (Entry Level) (New) (types=K; ...) :: ...
[2] OPQ32r (types=P; ...) :: ...
...
```

The LLM's only output for selection is a list of integer indices into this
list. Names and URLs are reconstructed from the catalog entries at those
indices.

A second defense layer — `agent/guardrails.py::_enforce_url_allowlist` —
validates that every emitted URL is in the catalog set before the response
is sent. Anything else is silently dropped.

## Alternatives considered

1. **LLM emits names; we fuzzy-match to the catalog.** Rejected: a fuzzy
   match between "Verify Numerical Reasoning" and "SHL Verify Interactive –
   Numerical Reasoning" can succeed *or* fail depending on the
   normalization, and getting it wrong silently silently reranks results.
2. **JSON schema validation requires `name` to be in an enum.** OpenAI's
   `response_format` with a JSON schema can do this in principle, but
   enums of 377 values blow the schema size and the JSON-mode behavior is
   inconsistent across providers.
3. **Trust the LLM with structured output and post-validate.** This is the
   common pattern in LangChain and similar; it puts the burden on a
   matcher we'd have to write and keep correct.

## Consequences

**Positive:**
- The agent **cannot** recommend a non-catalog assessment, by construction.
  The LLM has no way to introduce a new URL.
- Reduces the LLM's output token count substantially (a list of small
  integers vs. names + URLs + types).
- Easier prompt: the model just has to evaluate fit and pick relevant
  indices; it doesn't have to re-emit text.

**Negative:**
- Slightly more effort to format the candidate list well so the LLM has
  enough signal to differentiate similar entries (we include test types,
  job levels, length, and a 200-char description preview per candidate).
- The candidate list is part of the prompt, so it's bounded by token
  budget. We send up to top-10 candidates; the generator picks 1-10.
