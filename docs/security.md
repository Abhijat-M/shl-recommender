# Security & Threat Model

## Scope

A public, stateless HTTP service that consumes user text, calls a third-party
LLM, and returns text + structured data scraped from a public catalog.

The service holds **no PII**, **no authentication state**, and stores no data
on disk per request. The only secret is the Gemini API key.

## Assets

| Asset                          | Sensitivity | Notes                                    |
|--------------------------------|-------------|------------------------------------------|
| Gemini API key                 | High        | Loaded from env. Never logged or echoed. |
| Service availability           | Medium      | Ranking signal in SHL evaluation.        |
| Catalog data integrity         | Low         | Public information; baked at build time. |

## Threat model

### T1 — Prompt injection

**Threat:** A user (or a planted message in conversation history) instructs
the model to ignore its system prompt, fabricate URLs, leak the system
prompt, or change persona.

**Mitigations:**
1. **Local regex prefilter** (`agent/guardrails.py::INJECTION_PATTERNS`) —
   10 patterns catch common phrases ("ignore previous instructions",
   "developer mode", "DAN", "act as", "respond as", system-prompt reveals,
   etc.). Matches return a deterministic refusal **without** calling the
   LLM. Cheaper, faster, and impossible to bypass with prompt content.
2. **Router LLM as second classifier** — the system prompt explicitly lists
   prompt-injection as a refuse condition. JSON-mode keeps the output
   structured.
3. **LLM picks indices, not names** — the recommend prompt requires the
   LLM to reference candidates by their bracketed index in the supplied
   list. Even if the model hallucinated an output, names/URLs are
   reconstructed from the catalog, not the LLM.
4. **URL allowlist** — `agent/guardrails.py::filter_recommendations` drops
   any rec whose URL isn't in the catalog set. The schema layer caps to
   10 items as a final defense.
5. **Compare prompt is grounded** — the compare branch only feeds catalog
   records to the model and instructs it to say "I do not have that
   assessment in the catalog" rather than guess.

**Residual risk:** A novel injection phrase that bypasses both the regex
and the router classifier could change the model's tone but cannot make it
recommend a non-catalog assessment (the URL allowlist is deterministic).

### T2 — Off-topic abuse / general-purpose chat

**Threat:** Users ask for hiring advice, legal opinions, or generic chat,
inflating LLM costs.

**Mitigations:**
- The router prompt enumerates refuse reasons explicitly: `off_topic`,
  `legal`, `general_hiring`, `injection`. Each maps to a deterministic
  template in `REFUSAL_REPLIES`.
- **Local legal regex prefilter** (`looks_legal`, 10 patterns including
  "is it legal", "EEOC", "discrimination", "GDPR", "lawsuit") catches
  the largest abuse class (legal questions) before any LLM call. Match
  → 2 ms templated refusal.
- Off-topic regex prefilter (`looks_off_topic`) catches weather/recipe/
  movie/sports.
- Rate limiting (10 burst / 1 rps per IP) caps abuse cost.

### T3 — Denial of service

**Threat:** A caller floods `/chat` to exhaust quota or saturate the
instance.

**Mitigations:**
- **Token-bucket rate limiter** per client IP (default: 10 burst, 1 rps).
  Returns 429 with `Retry-After`.
- **Per-call LLM timeout** at 20 s, well inside the spec's 30 s.
- **Per-message size cap** at 8000 chars (truncated server-side).
- **Per-conversation turn cap** at 8 (history window trimmed before
  routing).
- **Local fast-path refuses** (injection, legal) catch the most common
  abuse without spending an LLM call.
- **Multi-model LLM fallback chain** (ADR-0008) so a single-model RPD
  exhaustion doesn't degrade service.
- **Retrieval-only fallback** (ADR-0009) so a total LLM outage doesn't
  block service or leak unbounded LLM cost.
- Stateless service — no memory leaks across requests.

**Residual risk:** A distributed flood from many IPs would slip past the
in-memory limiter. Move to a Redis-backed limiter or front with Cloudflare
for production-grade DDoS protection.

### T4 — Secret leakage

**Threat:** The Gemini API key is leaked through logs, error messages, or
response bodies.

**Mitigations:**
- The key is read from `GEMINI_API_KEY` env var via `pydantic-settings`
  and passed straight to the REST endpoint as a `?key=` query parameter
  (over HTTPS). Never logged.
- Exception messages from the SDK are caught and converted to `LLMError`
  with a sanitized message.
- `.env` is in `.gitignore`. `render.yaml` declares the key with
  `sync: false` so it is never written to the repo.
- The service never echoes request bodies in error responses.

### T5 — Server-side request forgery / catalog tampering

**Threat:** Someone manipulates the catalog so the agent recommends a
malicious URL.

**Mitigations:**
- The catalog is built at image build time from `scripts/scrape_catalog.py`.
  The output (`data/catalog/catalog.json`) is committed to the repo and
  reviewable in PRs.
- The URL allowlist is the *exact set* of URLs in the catalog snapshot —
  there is no way for the LLM to introduce a new URL at runtime.
- The scraper itself only reads from `shl.com` and writes JSON to disk; it
  is not invoked at request time.

### T6 — XSS / response-side injection

**Threat:** Malicious content from the catalog or user input renders as
HTML in a downstream client.

**Mitigations:**
- The service returns `application/json` only. The `Content-Security-Policy:
  default-src 'none'` header instructs browsers to block any embedded
  content from being executed.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` are always set.
- Encoded JSON is the responsibility of the consuming client to render
  safely; we don't ship a UI.

### T7 — CORS misuse

**Threat:** A malicious site loads the API in a victim's browser to
exfiltrate data through them.

**Mitigations:**
- `ALLOWED_ORIGINS` env var. Default is `*` for the SHL evaluator but
  should be tightened in any non-evaluation deployment.
- `allow_credentials=False` — cookies are never sent cross-origin.
- Only `GET`, `POST`, `OPTIONS` allowed.

### T8 — Dependency supply chain

**Threat:** A compromised dependency introduces malicious code.

**Mitigations:**
- All deps pinned in `requirements.txt` to exact versions.
- Pre-commit + CI run `ruff` and `mypy` to catch obvious code-quality
  regressions.
- The Docker image uses `python:3.11-slim` with a minimal apt set.

**Recommended additions for production:**
- Dependabot or Renovate for automated PRs on dep updates.
- `pip-audit` in CI to flag known CVEs.

## Sensitive operations checklist

When making changes to the codebase, watch for:

- [ ] Are you logging request bodies? Don't — they may contain candidate
      info.
- [ ] Are you echoing the API key in any response? Don't.
- [ ] Are you adding a new external HTTP call? Add it to the threat model.
- [ ] Are you weakening URL allowlist or schema validation? Don't.
- [ ] Are you adding a code path that bypasses the guardrails? Don't.

## Reporting a vulnerability

Open a private security advisory on GitHub or email
soccka@zohomail.in with subject `SECURITY: shl-recommender`.
