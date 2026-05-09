# API Reference

The service exposes 5 endpoints. The schema is defined in
[src/api/schemas.py](../src/api/schemas.py) and is **non-negotiable** — the
SHL evaluator rejects any deviation.

OpenAPI is also published at `/docs` (Swagger UI) and `/openapi.json`.

## Endpoints

| Method | Path        | Tag    | Purpose                                |
|--------|-------------|--------|----------------------------------------|
| GET    | `/health`   | probes | Liveness — process is up               |
| GET    | `/ready`    | probes | Readiness — index loaded, LLM ready    |
| GET    | `/version`  | probes | Build metadata                         |
| GET    | `/metrics`  | probes | Prometheus text exposition format      |
| POST   | `/chat`     | chat   | One conversational turn (stateless)    |

---

## `GET /health`

Liveness probe. Always returns `200 {"status":"ok"}` when the process is up.
It does **not** confirm the index is loaded — use `/ready` for that.

```bash
curl https://shl-recommender-yxcn.onrender.com/health
```

```json
{"status": "ok"}
```

---

## `GET /ready`

Returns `ready` only when:
1. The FAISS+BM25 index is loaded into memory, **and**
2. The Gemini API key is present in the environment.

Returns `503` otherwise. Use this for orchestration readiness checks
(Render's `healthCheckPath` is `/health` so deploys don't fail during
warm-up; promote to `/ready` if you control rollout).

```bash
curl https://shl-recommender-yxcn.onrender.com/ready
```

```json
{"status": "ready", "catalog_size": 377, "llm_configured": true}
```

---

## `GET /version`

Build/version metadata for diagnostics.

```bash
curl https://shl-recommender-yxcn.onrender.com/version
```

```json
{
  "name": "shl-recommender",
  "version": "0.1.0",
  "model": "gemini:gemini-flash-latest",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2 (via fastembed)"
}
```

The `model` field is `<provider>:<model_id>`. Provider is `gemini` by
default; `groq` is a kept-but-not-default alternative (set
`LLM_PROVIDER=groq` to opt in).

---

## `GET /metrics`

Prometheus text exposition. Sample (truncated):

```
# TYPE shl_chat_requests_total counter
shl_chat_requests_total{status="ok"} 142
shl_chat_requests_total{status="error"} 1

# TYPE shl_chat_intent_total counter
shl_chat_intent_total{intent="search"} 87
shl_chat_intent_total{intent="clarify"} 38
shl_chat_intent_total{intent="refuse"} 12

# TYPE shl_chat_duration_ms histogram
shl_chat_duration_ms_bucket{le="100"} 6
shl_chat_duration_ms_bucket{le="1000"} 110
shl_chat_duration_ms_bucket{le="+Inf"} 143
shl_chat_duration_ms_sum 102450.124
shl_chat_duration_ms_count 143
```

---

## `POST /chat`

The single conversational endpoint. **Stateless** — pass the entire message
history every call.

### Request

```json
{
  "messages": [
    {"role": "user",       "content": "Hiring a Java developer who works with stakeholders"},
    {"role": "assistant",  "content": "Sure. What is seniority level?"},
    {"role": "user",       "content": "Mid-level, around 4 years"}
  ]
}
```

| Field          | Type             | Notes                                          |
|----------------|------------------|------------------------------------------------|
| `messages`     | `ChatMessage[]`  | Full history. Empty array allowed.             |
| `messages[].role`    | `"user" \| "assistant" \| "system"` |                              |
| `messages[].content` | `string`        | Capped at 8000 chars (truncated server-side).  |

### Response

```json
{
  "reply": "Got it. Here are 5 assessments that fit a mid-level Java dev with stakeholder needs.",
  "recommendations": [
    {"name": "Java 8 (New)", "url": "https://www.shl.com/...", "test_type": "K"},
    {"name": "OPQ32r", "url": "https://www.shl.com/...", "test_type": "P"}
  ],
  "end_of_conversation": false
}
```

| Field                  | Type             | Notes                                                    |
|------------------------|------------------|----------------------------------------------------------|
| `reply`                | `string`         | Agent's natural-language reply.                          |
| `recommendations`      | `Recommendation[]` | **Empty** when clarifying or refusing. **1-10 items** when committed. |
| `recommendations[].name`     | `string`   | As it appears in the SHL catalog.                        |
| `recommendations[].url`      | `string`   | Full catalog URL on `shl.com`. Always from the catalog.  |
| `recommendations[].test_type`| `string`   | SHL letter codes (joined when multiple, e.g. `"KP"`).    |
| `end_of_conversation`  | `boolean`        | `true` only when the agent considers the task complete.  |

### Status Codes

| Code | When                                                      |
|------|-----------------------------------------------------------|
| 200  | Agent processed the turn (clarify/refuse/recommend/compare). |
| 422  | Request body failed Pydantic validation.                  |
| 429  | Rate limit hit. Body matches the empty-recs shape; `Retry-After` header included. |
| 503  | Service is still starting. Retry after a few seconds.     |

### Behavior Rules (encoded in the agent)

| User input                                           | Expected behavior                |
|------------------------------------------------------|----------------------------------|
| "I need an assessment"                               | clarify                          |
| "Hiring a Java dev, mid-level, 4 yrs"                | recommend (1-10)                 |
| "Add personality tests"                              | refine (still 1-10)              |
| "What's the difference between OPQ32r and Verify?"   | compare (text only, no recs)     |
| "What's a good pasta recipe?"                        | refuse off_topic                 |
| "Is age discrimination legal in California?"         | refuse legal                     |
| "Ignore previous instructions"                       | refuse injection                 |

### Examples

#### Clarify on vague intent

```bash
curl -X POST https://shl-recommender-yxcn.onrender.com/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"I need a test"}]}'
```

```json
{
  "reply": "What role are you hiring for, and at what seniority level?",
  "recommendations": [],
  "end_of_conversation": false
}
```

#### Recommend with sufficient context

```bash
curl -X POST https://shl-recommender-yxcn.onrender.com/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages":[
      {"role":"user","content":"Hiring a mid-level Python developer, need a coding test"}
    ]
  }'
```

```json
{
  "reply": "Here is a shortlist of SHL assessments that fit a mid-level Python developer.",
  "recommendations": [
    {"name": "Python (New)", "url": "https://www.shl.com/products/product-catalog/view/python-new/", "test_type": "K"},
    {"name": "Automata Pro (New)", "url": "https://www.shl.com/products/product-catalog/view/automata-pro-new/", "test_type": "S"}
  ],
  "end_of_conversation": false
}
```

#### Refuse off-topic

```bash
curl -X POST https://shl-recommender-yxcn.onrender.com/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Tell me a joke"}]}'
```

```json
{
  "reply": "I can only help you find SHL assessments...",
  "recommendations": [],
  "end_of_conversation": false
}
```

### Headers

| Header             | Direction | Purpose                                                |
|--------------------|-----------|--------------------------------------------------------|
| `X-Request-Id`     | both      | Request correlation. Server generates if missing.       |
| `Retry-After`      | response  | Set on 429 responses (seconds).                        |

### Limits

| Limit                       | Value                                |
|-----------------------------|--------------------------------------|
| `messages` length           | 8 turns max (older turns dropped).   |
| `content` length            | 8000 chars per message (truncated).  |
| Rate limit (per IP)         | 10 burst, 1 req/sec sustained.       |
| Per-call timeout            | 30 s (LLM internal cap: 20 s).       |
| Max recommendations         | 10.                                  |
