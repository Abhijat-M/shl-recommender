# ADR 0007: In-memory rate limiter and metrics (single-instance)

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

Rate limiting and metrics need somewhere to keep state. The textbook answer
for a horizontally-scaled deployment is Redis (limiter) + an external
metrics backend (Prometheus, Datadog).

For the SHL deployment we run **one** Render Free instance. The cost,
latency, and operational complexity of an external Redis are out of
proportion to the benefit at this scale.

## Decision

* **Rate limiter:** In-process token bucket (`src/rate_limit.py`) keyed by
  client IP (`X-Forwarded-For` first, falling back to socket peer).
  Threadsafe via a single `Lock`.

* **Metrics:** In-process registry (`src/metrics.py`) with counters and
  histograms. Exported in Prometheus text format via `GET /metrics`.

Both are documented in their module docstrings as "swap to Redis /
prometheus_client when going multi-instance."

## Alternatives considered

1. **Redis-backed limiter + `prometheus_client`.** Production-correct, but
   a third dependency, a managed service to pay for, and another failure
   surface. Overkill for one instance.
2. **`slowapi` package.** Same in-memory model as ours but adds an external
   dependency for ~80 lines of equivalent code. Skipped to keep deps tight.
3. **Skip rate limiting entirely.** Acceptable for a take-home running
   behind a controlled evaluator, but the project is "production-grade" so
   we include the layer.

## Consequences

**Positive:**
- Zero new dependencies. Zero external services. Zero ops burden.
- The implementation is small enough to read end-to-end in 5 minutes.
- The Prometheus text format is stable, so the same scrape config works
  if/when we swap the backend.

**Negative:**
- **Per-instance only.** With multiple replicas, each enforces its own
  bucket; effective limit is `N × budget`. Acceptable for free-tier
  deployment; a real upgrade path is documented.
- **Lost on restart.** Rate-limit buckets and metric counters reset to zero
  whenever the process restarts. For us: the SHL evaluator and idle-sleep
  Render Free both restart frequently anyway, so cumulative metrics are
  scoped per-deployment.
- **Memory growth bounded by `evict_idle`.** The limiter's `evict_idle`
  is exposed but not wired to a periodic task; if you observe memory
  growth in long-running deployments, schedule it via APScheduler or a
  background asyncio task.
