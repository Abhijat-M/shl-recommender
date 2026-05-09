# ADR 0010: Embeddings via fastembed (ONNX Runtime), not sentence-transformers

* **Status:** Accepted
* **Date:** 2026-05-09
* **Supersedes:** the original choice in ADR-0002

## Context

ADR-0002 picked `sentence-transformers/all-MiniLM-L6-v2` for dense
retrieval. That choice was correct *for the model* — MiniLM-L6-v2 is the
right size and quality for a 377-item catalog. The problem was the
**runtime**.

`sentence-transformers` pulls in the full PyTorch + transformers stack:

| Component | Resident memory |
|-----------|----------------:|
| Python + uvicorn + FastAPI baseline | ~80 MB |
| FAISS index + BM25 + catalog | ~15 MB |
| **PyTorch + transformers + sentence-transformers** | **~300-400 MB** |
| **Total** | **~500-600 MB** |

The first deploy to Render Free **OOM-killed** at startup with the dashboard
message: *"Ran out of memory (used over 512MB) while running your code."*

We didn't want to drop dense retrieval (BM25-only loses ~10-15% recall on
paraphrase queries) and we didn't want to pay for a bigger Render plan.

## Decision

Switch the embedding runtime from `sentence-transformers` to
[`fastembed`](https://github.com/qdrant/fastembed):

- **Same model.** fastembed wraps the same MiniLM-L6-v2 weights as an
  ONNX export (`Qdrant/all-MiniLM-L6-v2-onnx` on HuggingFace), so the
  embeddings are byte-identical and our existing FAISS index is reused
  without rebuilding.
- **ONNX Runtime instead of PyTorch.** ONNX Runtime is ~3× lighter
  resident than PyTorch's eager-mode runtime and starts faster.
- **Drop-in API.** `TextEmbedding(model_name).embed(texts)` returns a
  generator of `np.ndarray` rows; we collect into a stack and L2-
  normalize, identical to before.

### Implementation notes

- `cache_dir` is passed **explicitly** to `TextEmbedding(...)` from the
  `FASTEMBED_CACHE_DIR` env var. fastembed's own env-var name is not
  stable across versions; passing the kwarg keeps us provider-honest.
- The Dockerfile **bakes the ONNX weights** into the image at build time
  (under `/opt/fastembed_cache`) so the runtime container does not need
  network access to start.
- We removed `sentence-transformers`, `torch`, and `transformers` from
  `requirements.txt`. `onnxruntime` (a fastembed dep) replaces them.

## Alternatives considered

1. **Stay on sentence-transformers, upgrade to Render Starter ($7/mo).**
   Buys 2 GB RAM. Rejected: the spec says free-tier-friendly and we want
   to keep the project free-tier-friendly for future maintainers.
2. **Drop dense retrieval entirely; use BM25-only.** ~10-15% recall@10
   loss on paraphrase queries. Rejected: too much quality cost.
3. **Use Gemini's `text-embedding-004` API for both build and runtime.**
   Removes all ML libraries from runtime entirely. Rejected: requires
   `GEMINI_API_KEY` at *build* time too (complicates Docker), and adds
   ~50-100 ms per query for the API round trip. Pure local is preferable.
4. **Pre-compute embeddings via sentence-transformers in the builder
   stage; never load any embedding library at runtime.** Catalog vectors
   work, but query-time embeddings still need a runtime model — so the
   library is unavoidable.

## Consequences

**Positive:**
- Runtime resident memory: **~180-250 MB** (was ~500-600 MB) — fits
  Render Free's 512 MB cap with headroom.
- `pytest` runtime: **~12 s** (was ~100-150 s) because there's no
  PyTorch import to wait on at fixture-load.
- Image size: ~600 MB smaller (no torch wheel).
- Cold start: ~5 s faster.
- Embeddings unchanged byte-for-byte → no quality regression, no
  re-embedding required.

**Negative:**
- ONNX Runtime has a smaller ecosystem than PyTorch — fewer drop-in
  alternatives if fastembed becomes unmaintained. Mitigated by the
  `LLMClient`-style insulation: only `_embed()` and `Retriever.load()`
  touch the embedding library; swapping back to sentence-transformers is
  a 20-line revert.
- The build now depends on `huggingface_hub` reaching `huggingface.co`
  during `docker build` to fetch the ONNX weights. Render's build
  environment has internet, so this is fine in practice; in a fully
  air-gapped build, the ONNX weights would need to be vendored.

## Tests + verification

- `tests/test_retrieval.py` — unchanged; still passes.
- `tests/test_orchestrator.py` — unchanged; still passes.
- All 107 existing tests pass after the swap.
- Live verification on the deployed Render service:
  - `/health` returns 200
  - `/ready` reports `catalog_size: 377, llm_configured: true`
  - `POST /chat` returns 5 K-typed Java assessments for a Java-dev
    query, identical to local behavior pre-swap.
