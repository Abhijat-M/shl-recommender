# ADR 0001: Stateless API with per-turn state derivation

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The SHL spec requires the `/chat` endpoint to be stateless: the client
sends the entire message history every call, and the service holds no
per-conversation memory.

This is at odds with how most agent frameworks (LangGraph, OpenAI Assistants
API, etc.) think about state — they assume a persistent thread.

## Decision

Treat each `POST /chat` as a pure function of the input history. Derive
the agent's working state (slots, intent, search query, prior shortlist)
**from the messages on every turn** by asking the router LLM to extract it.

The orchestrator does not read or write any persistent store.

## Alternatives considered

1. **Session store keyed by a client-supplied conversation ID.** Rejected:
   violates the spec; introduces persistence and TTL concerns; and the
   evaluator harness would need to opt in.
2. **Embed agent state in `assistant` messages (e.g. JSON in metadata).**
   Rejected: contaminates the wire format; the spec defines a strict shape
   for assistant turns and there's no metadata channel.
3. **Maintain state via the `system` message.** Rejected: the evaluator
   may not include any system messages at all in its replay.

## Consequences

**Positive:**
- Trivial to scale horizontally (add more instances; no shared state).
- Crash-safe: a process restart loses nothing.
- Test isolation is automatic (every test is a fresh "session").

**Negative:**
- Each turn pays the cost of re-extracting slots from history. We mitigate
  this with the LRU retrieval cache and Gemini Flash's sub-second latency.
- The agent can theoretically miss a constraint that was implied many
  turns ago. Mitigation: the router prompt instructs the model to consider
  the *full* history, and the 8-turn cap (also from the spec) keeps the
  prompt size bounded.
