# ADR 0006: Bake catalog + index into the Docker image

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The service needs the catalog (`catalog.json`) and the retrieval index
(FAISS + BM25) at request time. Two options:

1. Build them in the running container at startup.
2. Build them at *image build* time and ship a ready-to-serve image.

Render's free tier sleeps after 15 min idle. The SHL evaluator allows up
to 2 minutes for the first `/health` call, but every second of cold start
is also a second the first `/chat` is delayed.

## Decision

The Docker image bakes:
- `data/catalog/catalog.json` (committed to the repo).
- `data/index/dense.faiss`, `bm25.pkl`, `documents.pkl`, `meta.pkl`
  (built by `scripts/build_index.py` during `docker build`).

The running container only fetches the embedding model
(`all-MiniLM-L6-v2` ONNX export via fastembed, ~80 MB) — the weights
are now baked into the image at build time (see ADR-0010), cached
under `HF_HOME=/tmp/hf`.

A multi-stage Dockerfile separates the build environment (heavy ML deps
needed for `build_index.py`) from the runtime environment (slim, just runs
uvicorn).

## Alternatives considered

1. **Build the index at startup.** Adds ~5 s to cold start. More importantly,
   it requires the running container to have `build_index.py`'s heavy deps,
   which inflates the image.
2. **Build the index in a sidecar volume.** Render Free doesn't support
   persistent volumes; rebuilding on every redeploy was effectively the
   same cost.
3. **Ship the embedding model in the image too.** Considered. ~80 MB of
   model + tokenizer files. Adds image size and image-pull time. The HF
   Hub fetch is fast (~3-5 s on Render) and only happens once per cold
   start. Net: leave it out of the image.
4. **Don't commit `catalog.json`; rebuild at deploy time.** Requires the
   build environment to have network access to shl.com and accept the
   risk that shl.com is down or its HTML drifted at deploy time. We prefer
   determinism; refresh the catalog explicitly via a PR.

## Consequences

**Positive:**
- Cold start is ~6 s (including model download), well under the 2-min cap.
- The running image is small (~700 MB; minus the model would be ~600 MB).
- Catalog refreshes happen via PR → reviewable diff in `catalog.json`.

**Negative:**
- The image must be rebuilt to pick up catalog changes (a `git push` is
  enough; Render's autoDeploy handles the rest).
- The Docker build is heavier — multi-stage adds a few seconds, plus the
  index-build step (~30 s).
