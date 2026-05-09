"""Build FAISS dense index + BM25 sparse index from a catalog file.

Run as:
    python scripts/build_index.py

The index is written to data/index/ and consumed by the retriever at runtime.
We use:
- IndexFlatIP over normalized vectors (== cosine similarity), since the
  catalog is small (~500 items) and exact search at this scale is sub-ms.
- rank_bm25 BM25Okapi for sparse retrieval. We persist the tokenized corpus.
"""

from __future__ import annotations

import logging
import pickle
import re
from pathlib import Path

import numpy as np

from src.retrieval.catalog import Assessment, load_catalog

LOG = logging.getLogger(__name__)

# Paths (relative to index_dir)
DENSE_INDEX_FILE = "dense.faiss"
EMBEDDINGS_FILE = "embeddings.npy"
DOCS_FILE = "documents.pkl"
BM25_FILE = "bm25.pkl"
META_FILE = "meta.pkl"

# Tokenization for BM25
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase + alphanumeric word tokenization for BM25."""
    return [t.lower() for t in TOKEN_RE.findall(text)]


def _normalize(arr: np.ndarray) -> np.ndarray:
    """L2-normalize each row in-place; safe against zero-rows."""
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return arr / norms


def _embed(model_name: str, texts: list[str]) -> np.ndarray:
    """Embed a batch of texts via fastembed (ONNX Runtime).

    fastembed is a drop-in replacement for sentence-transformers that uses
    ONNX Runtime instead of PyTorch — ~3x lighter resident memory, which
    matters on hosts with a 512 MB cap (e.g. Render Free).

    `FASTEMBED_CACHE_DIR` (env var, optional) controls where the ONNX
    weights are cached. We pass it explicitly so a multi-stage Docker
    build can pre-cache in the builder and COPY into the runtime image.

    The function is local-imported so command-line tools that don't touch
    embeddings don't pay the import cost.
    """
    import os

    from fastembed import TextEmbedding

    cache_dir = os.environ.get("FASTEMBED_CACHE_DIR") or None
    model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
    LOG.info(
        "Embedding %d documents with %s (fastembed, cache=%s)",
        len(texts),
        model_name,
        cache_dir or "<default>",
    )
    # fastembed.embed() is a generator yielding np.ndarray rows.
    embeddings = np.asarray(list(model.embed(texts)), dtype=np.float32)
    return embeddings


def build(catalog_path: str | Path, index_dir: str | Path, model_name: str) -> int:
    """Build dense + sparse indices from `catalog_path` into `index_dir`.

    Returns the number of indexed assessments.
    """
    import faiss
    from rank_bm25 import BM25Okapi

    catalog: list[Assessment] = load_catalog(catalog_path)
    if not catalog:
        raise RuntimeError(
            f"Catalog at {catalog_path} is empty. Run scripts/scrape_catalog.py first."
        )

    out_dir = Path(index_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = [a.search_text() for a in catalog]

    # Dense index
    raw = _embed(model_name, docs)
    embeddings = _normalize(raw)
    dim = embeddings.shape[1]
    LOG.info("Building FAISS index dim=%d n=%d", dim, len(catalog))
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)
    faiss.write_index(index, str(out_dir / DENSE_INDEX_FILE))
    np.save(out_dir / EMBEDDINGS_FILE, embeddings)

    # Sparse index
    tokenized = [tokenize(d) for d in docs]
    bm25 = BM25Okapi(tokenized)
    with open(out_dir / BM25_FILE, "wb") as f:
        pickle.dump({"bm25": bm25, "tokenized": tokenized}, f)

    # Sidecar: docs and metadata (catalog snapshot, model name)
    with open(out_dir / DOCS_FILE, "wb") as f:
        pickle.dump(docs, f)
    with open(out_dir / META_FILE, "wb") as f:
        pickle.dump(
            {
                "model_name": model_name,
                "catalog": catalog,  # full Assessment objects
                "n": len(catalog),
            },
            f,
        )
    LOG.info("Index built: %d items in %s", len(catalog), out_dir)
    return len(catalog)


def index_files_present(index_dir: str | Path) -> bool:
    out = Path(index_dir)
    needed = [DENSE_INDEX_FILE, BM25_FILE, DOCS_FILE, META_FILE]
    return all((out / f).exists() for f in needed)
