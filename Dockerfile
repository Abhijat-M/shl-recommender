# Multi-stage Dockerfile for the SHL Recommender.
#
# Stage 1 ("builder"): install Python deps and pre-bake the FAISS+BM25 index
# from the catalog committed to the repo. This avoids hitting shl.com at
# container startup and keeps cold starts fast.
#
# Stage 2 ("runtime"): minimal image with just deps + code + index + catalog.
#
# Build:   docker build -t shl-recommender .
# Run:     docker run --rm -p 8000:8000 -e GROQ_API_KEY=... shl-recommender
#
# The image bakes in `data/catalog/catalog.json` and the built index, so the
# container starts up serving immediately.

FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for sentence-transformers / faiss / lxml
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

# Pre-build the FAISS+BM25 index inside the image so cold start is fast.
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

# Copy app + pre-built index + catalog.
COPY --from=builder /app/src ./src
COPY --from=builder /app/data ./data

# Embed-model cache lives under HF cache. We avoid baking it (it's ~90 MB)
# but pre-warm at startup to keep first /chat fast.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HF_HOME=/tmp/hf

EXPOSE 8000

# Render injects $PORT, otherwise default 8000.
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
