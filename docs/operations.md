# Operations Runbook

This runbook is for the on-call engineer. It assumes the service is deployed
to Render via `render.yaml`. Most procedures generalize to any Docker host.

## Environment variables

| Variable                | Required | Default                                       | Notes |
|-------------------------|----------|-----------------------------------------------|-------|
| `GEMINI_API_KEY`        | yes      | —                                             | https://aistudio.google.com/app/apikey |
| `GEMINI_MODEL`          | no       | `gemini-flash-latest`                         | Free-tier candidates: `gemini-flash-latest`, `gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-2.5-flash-lite` (each has its own 20 RPD quota) |
| `GEMINI_FALLBACK_MODELS`| no       | `gemini-2.5-flash,gemini-2.0-flash,gemini-2.5-flash-lite` | Comma-separated model chain. On a daily-quota 429 we slide to the next; per-minute 429s still retry on the same model. |
| `EMBEDDING_MODEL`       | no       | `sentence-transformers/all-MiniLM-L6-v2`      | Loaded via fastembed (ONNX Runtime). Must match the index built into the image. |
| `FASTEMBED_CACHE_DIR`   | no       | `/opt/fastembed_cache` (set in Dockerfile)    | Where the ONNX weights are cached. The Docker image bakes them at build time so runtime needs no network. |
| `APP_HOST`              | no       | `0.0.0.0`                                     | |
| `APP_PORT` / `PORT`     | no       | `8000` / `10000` (Render)                     | Render injects `PORT`; we honor it |
| `LOG_LEVEL`             | no       | `INFO`                                        | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_JSON`              | no       | `1`                                           | `0` for human-readable logs |
| `ALLOWED_ORIGINS`       | no       | `*`                                           | Comma-separated origins; tighten in production |
| `RATE_LIMIT_BURST`      | no       | `10`                                          | Token-bucket capacity per IP |
| `RATE_LIMIT_RPS`        | no       | `1`                                           | Sustained refill rate per IP |
| `MAX_TURNS`             | no       | `8`                                           | Hard cap from SHL spec |
| `LLM_TIMEOUT_SECONDS`   | no       | `20`                                          | Internal cap; spec allows 30 |
| `TOP_K_DENSE`           | no       | `25`                                          | FAISS top-K |
| `TOP_K_SPARSE`          | no       | `25`                                          | BM25 top-K |
| `TOP_K_FINAL`           | no       | `10`                                          | Returned to LLM (and final cap) |
| `RRF_K`                 | no       | `60`                                          | RRF dampening constant |
| `CATALOG_PATH`          | no       | `data/catalog/catalog.json`                   | Relative to project root |
| `INDEX_DIR`             | no       | `data/index`                                  | Relative to project root |
| `HF_HOME`               | no       | `/tmp/hf`                                     | HuggingFace hub cache (used by fastembed under the hood when downloading the ONNX weights at build time). |

## Deploy to Render (first time)

1. Push the repo to GitHub.
2. On render.com → **New** → **Blueprint** → connect your repo.
3. The `render.yaml` file is auto-detected; review the service.
4. Before the first deploy, **set `GEMINI_API_KEY`** in the dashboard (envVars
   section).
5. Deploy. First build takes ~10 minutes (heavy ML wheels). The build runs
   `scripts/build_index.py` so the image ships with both catalog and index.
6. After deploy, hit:
   * `GET /health` → expect `200 {"status":"ok"}`
   * `GET /ready` → expect `200 {"status":"ready","catalog_size":377,...}`
   * `GET /version` → confirms model + version

## Deploy via Docker (any host)

```bash
docker build -t shl-recommender .
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY=$GEMINI_API_KEY \
  shl-recommender
```

## Routine operations

### Refresh the catalog

The SHL catalog changes occasionally (new assessments, retired ones).
Refresh procedure:

```bash
python scripts/scrape_catalog.py
python scripts/build_index.py
git add data/catalog/catalog.json
git commit -m "chore(catalog): refresh from shl.com"
git push
```

The next deploy ships the new catalog. The image rebuilds the index from
catalog.json automatically.

### Tune retrieval

Most retrieval issues are fixable without a deploy by adjusting env vars:

| Symptom                                      | Knob                              |
|----------------------------------------------|-----------------------------------|
| Recall@10 too low; agent picks duplicates    | Bump `TOP_K_DENSE` / `TOP_K_SPARSE` |
| Agent is too eager (skipping clarify)        | Tighten router prompt (code change) |
| Hot queries are slow                         | `cache_size` argument to retriever (code change; default 128) |

### Roll back

Render: open the **Manual Deploy** menu → pick the previous successful
build → **Redeploy**. No data migration required (service is stateless).

For Docker: pull the previous tag and restart.

## Incidents

### "All requests return 503"

Cause: orchestrator failed to warm at startup.

Triage:
1. `GET /ready` → look at body (`catalog_size`, `llm_configured`).
2. Check logs for `orchestrator_warm_failed`. The exception is logged.
3. Common causes:
   - **Missing `GEMINI_API_KEY`** → set env var, restart.
   - **Index files missing** → image build skipped `build_index.py`.
     Inspect the Render build log; rebuild the image.
   - **Catalog file empty** → re-run `scripts/scrape_catalog.py` and commit.

### "Health is OK but /chat returns the fallback message"

There are now **two** kinds of fallback responses, with different causes.

**A. "Here are SHL assessments that look most relevant to your query."**
This is the ADR-0009 retrieval-only fallback. It means the LLM router
failed entirely (after exhausting the multi-model chain) but the local
retriever still produced sensible recs. The agent is still useful, but
the LLM-classified intent (clarify / search / refine / compare) is lost.

**B. "Sure - tell me a bit more. What role are you hiring for…"**
This is the canned `initial_clarifying_question()`. It means the LLM
failed AND retrieval also produced nothing — usually because the user
input was very short.

Triage either case:
1. `/version` → confirms `provider:model` actually selected.
2. Logs to grep:
   - `router LLM failed` — primary symptom.
   - `Gemini daily quota exhausted on <model>; trying next.` — chain
     fallthrough working as intended.
   - `Gemini 429` (no daily marker) → per-minute throttle; tenacity
     retries internally.
   - `Gemini returned non-JSON: '...'` → output truncated; bump
     `max_tokens` in the orchestrator (router default 1500, recommend 800).
3. If `LLMError: GEMINI_API_KEY is not set` → fix env.
4. If every model is daily-quota exhausted (`Gemini daily quota
   exhausted` in logs for **all** chain entries):
   - The free-tier limit is 20 RPD per model. Wait for the daily reset
     (UTC midnight) or upgrade billing.
   - Add another model to `GEMINI_FALLBACK_MODELS` to extend the chain.
5. If 404 from Gemini ("model not found") → list available models with
   `python scripts/probe_gemini_models.py`. Common cause: an old model
   name like `gemini-1.5-flash` that's been retired.

The agent will keep emitting **schema-valid** responses regardless;
ADR-0009 guarantees no 5xx and no broken `recommendations` shape.

### "Latency p99 spiked"

Triage:
1. `GET /metrics` → check `shl_chat_duration_ms_bucket{le="5000"}` vs total.
2. Common causes:
   - **Cold start** after sleep on Render Free → expected; first call ~30 s.
   - **Gemini backend slow** → no fix server-side; consider switching model
     (e.g. `gemini-2.5-flash-lite` is faster than `gemini-2.5-flash`).
   - **Cache cold** → repeat queries should be < 100 ms after first hit.
3. The orchestrator caps each LLM call at `LLM_TIMEOUT_SECONDS` (default 20 s).
   The handler returns a fallback clarify if timeouts hit, never blocks.

### "Caller reports bad recommendations / hallucinated URL"

By design, the URL allowlist prevents this. If you see one anyway:
1. Capture the conversation (request + response, with `X-Request-Id`).
2. Reproduce locally:
   ```bash
   uvicorn src.main:app --reload --log-level debug
   curl -X POST http://localhost:8000/chat -d @reproduction.json
   ```
3. Inspect the orchestrator's `rationale` (logged with each turn).
4. If the rec slipped through, add a regression test in `tests/test_api.py`.

### "Rate limited unexpectedly"

The default is generous (10 burst, 1 rps per IP). If a legitimate caller
hits 429:
1. Check `X-Forwarded-For` is set correctly behind your proxy.
2. Bump `RATE_LIMIT_BURST` or `RATE_LIMIT_RPS` env vars.
3. For multi-instance deployments, swap the in-memory limiter for a
   Redis-backed one (see `src/rate_limit.py` notes).

## Observability

### Logs

All logs are JSON-line. Standard fields: `ts`, `level`, `logger`, `event`,
`request_id`, `attrs`. Filter by request:

```bash
# Render shell:
journalctl -u shl-recommender | jq -c 'select(.request_id == "abcd1234")'
```

### Metrics

`/metrics` is Prometheus-compatible. Recommended scrapes:

| Metric                             | Alert when…                         |
|------------------------------------|--------------------------------------|
| `shl_chat_requests_total{status="error"}` | rate > 5/min                  |
| `shl_chat_duration_ms{quantile=0.95}`     | > 8000 ms for 5 min          |
| `shl_chat_intent_total{intent="refuse"}`  | > 30% of all turns            |
| `shl_chat_recs_returned_total`            | suddenly drops to 0           |

### Request tracing

Pass `X-Request-Id` from your client. The server echoes it back and embeds
it in every log line.

## Verifying capabilities live

`scripts/verify_capabilities.py` exercises the 5 spec behaviors against
a running service and asserts on the responses:

```bash
python scripts/verify_capabilities.py --base-url http://127.0.0.1:8000
```

It tests:
1. Non-linear conversation (skill before role, mid-conversation correction)
2. Job description blob on turn 1 (recommend immediately)
3. 8-turn drip (carry context across turns)
4. Grounded compare (catalog facts only, no recs returned)
5. Resilience (empty / oversize / odd inputs always return 200 + valid schema)

For automated probe-style tests against the live service, see
`scripts/eval.py --probes-only`.

## Capacity

Render Free: 1 instance, ~512 MB RAM, sleeps after 15 min idle.
- Cold start: ~5 s (catalog/index baked) + ~1 s embedding model load.
- Throughput: limited by Gemini free-tier RPM (15 RPM per model;
  ~60 RPM across the 4-model chain). The retrieval-only fallback (ADR-0009)
  handles bursts beyond that without 429ing the caller.
- Memory: ~250 MB resident with index loaded.

To scale: move to Render Starter ($7/mo, no sleep) or Fly.io with multiple
machines. Replace in-memory rate limiter and metrics with shared backends.
