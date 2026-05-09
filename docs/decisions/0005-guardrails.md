# ADR 0005: Defense-in-depth guardrails

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The SHL evaluator includes behavior probes for prompt-injection resistance,
off-topic refusal, and hallucination rate. A single defensive layer is
fragile — an attacker need only find one phrasing that bypasses it.

## Decision

Three layers, in order of execution:

1. **Local regex prefilter** (`agent/guardrails.py::is_obviously_injection`).
   Run on the *latest user message* before the LLM is invoked. 10 patterns
   cover known jailbreak and persona-shift phrases. Match → deterministic
   refusal text, no LLM call.

2. **Router LLM as classifier.** The system prompt explicitly enumerates
   refuse conditions: `off_topic`, `injection`, `legal`, `general_hiring`.
   When the router returns `intent=refuse`, the orchestrator emits a
   deterministic template — the LLM does not draft refusal text.

3. **URL allowlist** (`agent/guardrails.py::filter_recommendations`). After
   the generator picks indices, the orchestrator reconstructs name/URL
   pairs from the catalog and **re-validates** that every URL is in the
   catalog set. The schema layer caps the list at 10.

## Alternatives considered

1. **Trust the LLM alone.** Tried; broke on phrasings like "let's roleplay
   as a SQL injector". The local regex catches several of these even
   without invoking the LLM.
2. **Train a small classifier** for prompt-injection detection. Better
   recall in principle but adds another model and another training cost.
   Out of scope for a take-home.
3. **A single allowlist of all catalog hosts** instead of full URLs.
   Considered, but a host allowlist is broader than needed (the agent could
   theoretically emit `https://www.shl.com/random/page/`). Full-URL
   allowlist is exact.

## Consequences

**Positive:**
- Cheaper: obvious attacks never reach the LLM (the regex is a few µs).
- More predictable refusal text (templated, not generated).
- Defense in depth: even if one layer is bypassed, the next catches it.

**Negative:**
- The regex is brittle to novel phrasings. We must keep it updated when
  new prompt-injection patterns surface.
- Refusal templates are slightly canned. We accept this — the spec values
  consistency over literary variety.
