# Multi-stage Dockerfile for the SHL Recommender.
#
# Stage 1 ("builder"): install Python deps and pre-bake the FAISS+BM25 index
# from the catalog committed to the repo. This also caches the fastembed
# ONNX model into /opt/fastembed_cache so the runtime image starts cold-fast.
#
# Stage 2 ("runtime"): minimal image with just deps + code + index + catalog +
# pre-cached ONNX model. Targets Render Free (512 MB RAM).
#
# Build:   docker build -t shl-recommender .
# Run:     docker run --rm -p 8000:8000 -e GEMINI_API_KEY=... shl-recommender
#
# The image bakes in `data/catalog/catalog.json`, the built index, AND the
# fastembed ONNX model so the container serves immediately with no network.

FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for faiss / lxml. fastembed (ONNX Runtime) needs no compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Bring in the source + scraped catalog (catalog.json is committed; index is built here).
COPY src ./src
COPY scripts ./scripts
COPY data/catalog ./data/catalog

# Pre-build the FAISS+BM25 index inside the image. fastembed downloads the
# ONNX model into FASTEMBED_CACHE on first use; we direct it to a stable
# path that we copy into the runtime image.
ENV FASTEMBED_CACHE=/opt/fastembed_cache
RUN python scripts/build_index.py

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy app + pre-built index + catalog + fastembed ONNX model cache.
COPY --from=builder /app/src ./src
COPY --from=builder /app/data ./data
COPY --from=builder /opt/fastembed_cache /opt/fastembed_cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    FASTEMBED_CACHE=/opt/fastembed_cache

EXPOSE 8000

# Render injects $PORT, otherwise default 8000.
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
