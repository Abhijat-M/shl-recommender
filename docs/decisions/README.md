# Architecture Decision Records

Each ADR captures one significant architectural choice and the trade-offs
considered. Format is `<MADR>` (Markdown ADR) with status, context,
decision, consequences.

| ADR | Title                                                       | Status   |
|-----|-------------------------------------------------------------|----------|
| 0001 | [Stateless API with per-turn state derivation](0001-stateless-api.md) | Accepted |
| 0002 | [Hybrid retrieval: FAISS dense + BM25 sparse + RRF](0002-hybrid-retrieval.md) | Accepted |
| 0003 | [LLM picks indices, not names (URL allowlist)](0003-index-not-name.md) | Accepted |
| 0004 | [Two-stage agent: router + generator](0004-two-stage-agent.md) | Accepted |
| 0005 | [Defense-in-depth guardrails (regex + LLM + allowlist)](0005-guardrails.md) | Accepted |
| 0006 | [Bake catalog + index into Docker image](0006-bake-catalog.md) | Accepted |
| 0007 | [In-memory rate limiter and metrics](0007-in-memory-state.md) | Accepted |
| 0008 | [Gemini LLM provider with multi-model fallback](0008-pluggable-llm-provider.md) | Accepted |
| 0009 | [Graceful degradation when the LLM is unavailable](0009-graceful-degradation.md) | Accepted |
