"""Hybrid retriever (dense FAISS + sparse BM25, fused with RRF).

Loaded once at app startup. Stateless per-request: takes a query string
(plus optional filters) and returns a ranked list of catalog Assessments.

Why hybrid:
- Dense retrieval (MiniLM via fastembed/ONNX Runtime) generalizes across
  vocabulary mismatches: "Java developer" matches "Java 8 (New)" even
  though the surface forms differ. fastembed (ONNX) keeps resident memory
  ~3x lower than sentence-transformers (PyTorch) — required for Render
  Free's 512 MB cap.
- BM25 catches exact terms the embedding flattens: "OPQ32r", "Verify",
  product codes, version numbers.
- Reciprocal rank fusion (RRF) is simple, score-free, and robust.
"""

from __future__ import annotations

import logging
import pickle
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.config import get_settings
from src.retrieval.cache import LRUCache
from src.retrieval.catalog import Assessment
from src.retrieval.indexer import (
    BM25_FILE,
    DENSE_INDEX_FILE,
    DOCS_FILE,
    META_FILE,
    index_files_present,
    tokenize,
)

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievalFilters:
    """Optional hard filters applied after fusion."""

    test_types: set[str] = field(default_factory=set)
    require_remote_testing: bool | None = None
    require_adaptive: bool | None = None


@dataclass(slots=True)
class Hit:
    assessment: Assessment
    score: float  # fused (RRF) score
    dense_rank: int | None = None
    sparse_rank: int | None = None


class HybridRetriever:
    """Loads dense+sparse indices and serves ranked retrieval."""

    _lock = threading.Lock()

    def __init__(self, index_dir: str | Path | None = None, cache_size: int = 128) -> None:
        settings = get_settings()
        self._dir = Path(index_dir or settings.index_path)
        # The library types for faiss / fastembed / rank-bm25 are absent or
        # unstable; we type these as `Any` and rely on runtime guards.
        self._dense: Any = None
        self._bm25: Any = None
        self._tokenized: list[list[str]] = []
        self._docs: list[str] = []
        self._catalog: list[Assessment] = []
        self._embed_model: Any = None
        self._top_k_dense = settings.top_k_dense
        self._top_k_sparse = settings.top_k_sparse
        self._top_k_final = settings.top_k_final
        self._rrf_k = settings.rrf_k
        self._cache: LRUCache = LRUCache(capacity=cache_size)

    # ----- lifecycle -----
    def load(self) -> HybridRetriever:
        """Load all artifacts. Idempotent under the class lock."""
        if self._dense is not None:
            return self
        with self._lock:
            if self._dense is not None:
                return self
            if not index_files_present(self._dir):
                raise FileNotFoundError(
                    f"Index directory {self._dir} is missing required files. "
                    f"Run scripts/build_index.py first."
                )

            import faiss
            from fastembed import TextEmbedding

            with open(self._dir / META_FILE, "rb") as f:
                meta = pickle.load(f)
            self._catalog = list(meta["catalog"])
            model_name = meta["model_name"]

            with open(self._dir / DOCS_FILE, "rb") as f:
                self._docs = pickle.load(f)

            with open(self._dir / BM25_FILE, "rb") as f:
                bm25_pack = pickle.load(f)
            self._bm25 = bm25_pack["bm25"]
            self._tokenized = bm25_pack["tokenized"]

            self._dense = faiss.read_index(str(self._dir / DENSE_INDEX_FILE))
            # fastembed wraps the same MiniLM weights via ONNX Runtime —
            # ~3x smaller resident memory than sentence-transformers.
            self._embed_model = TextEmbedding(model_name=model_name)

            LOG.info(
                "Retriever loaded: %d items, model=%s (fastembed)",
                len(self._catalog),
                model_name,
            )
        return self

    # ----- query -----
    def search(
        self,
        query: str,
        filters: RetrievalFilters | None = None,
        top_k: int | None = None,
    ) -> list[Hit]:
        """Return the top-K hits across the catalog for `query`.

        The result is cached by `(query, filters, top_k)` — repeated identical
        searches return in O(1). The cache is process-local; restart the
        service to invalidate.
        """
        if not query.strip():
            return []
        self.load()
        assert self._dense is not None and self._bm25 is not None

        k_final = top_k or self._top_k_final
        cache_key = _cache_key(query, filters, k_final)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        k_dense = self._top_k_dense
        k_sparse = self._top_k_sparse

        # Dense — fastembed.embed yields normalized vectors per-row.
        raw = np.asarray(
            list(self._embed_model.embed([query])), dtype=np.float32
        )
        # Re-normalize defensively (fastembed normalizes for MiniLM but we
        # don't want to assume that across model variants).
        norm = np.linalg.norm(raw, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1.0, norm)
        q_vec = (raw / norm).astype(np.float32)
        # IndexFlatIP returns descending scores
        scores, idxs = self._dense.search(q_vec, k_dense)
        dense_rank: dict[int, int] = {}
        for rank, idx in enumerate(idxs[0]):
            if idx >= 0:
                dense_rank[int(idx)] = rank

        # Sparse
        bm25_scores = self._bm25.get_scores(tokenize(query))
        sparse_top = np.argsort(-bm25_scores)[:k_sparse]
        sparse_rank: dict[int, int] = {
            int(i): r for r, i in enumerate(sparse_top) if bm25_scores[i] > 0
        }

        # RRF fusion
        candidates = set(dense_rank) | set(sparse_rank)
        fused: list[tuple[float, int]] = []
        for idx in candidates:
            score = 0.0
            if idx in dense_rank:
                score += 1.0 / (self._rrf_k + dense_rank[idx])
            if idx in sparse_rank:
                score += 1.0 / (self._rrf_k + sparse_rank[idx])
            fused.append((score, idx))
        fused.sort(reverse=True)

        # Apply filters and pack hits
        out: list[Hit] = []
        for score, idx in fused:
            asmt = self._catalog[idx]
            if filters and not _passes(asmt, filters):
                continue
            out.append(
                Hit(
                    assessment=asmt,
                    score=score,
                    dense_rank=dense_rank.get(idx),
                    sparse_rank=sparse_rank.get(idx),
                )
            )
            if len(out) >= k_final:
                break
        self._cache.put(cache_key, out)
        return out

    @property
    def cache_stats(self) -> dict[str, float | int]:
        return {
            "size": len(self._cache),
            "hits": self._cache.hits,
            "misses": self._cache.misses,
            "hit_rate": self._cache.hit_rate,
        }

    # Used by the agent for "compare" intent: lookup by name
    def lookup_by_names(self, names: list[str], fuzzy: bool = True) -> list[Assessment]:
        """Return Assessment objects matching the given names (case-insensitive).

        The catalog contains unicode dashes (en-dash, em-dash), trademark
        symbols, and parenthetical suffixes. Fuzzy lookup normalizes both
        sides (lowercase, strip punctuation, collapse whitespace) and falls
        back to token-overlap if a substring match fails.
        """
        self.load()
        idx_by_name: dict[str, Assessment] = {a.name.lower(): a for a in self._catalog}
        # Build a normalized index for fuzzy matching.
        idx_by_norm: dict[str, Assessment] = {
            _normalize_name(a.name): a for a in self._catalog
        }
        norm_tokens: list[tuple[set[str], Assessment]] = [
            (set(_normalize_name(a.name).split()), a) for a in self._catalog
        ]

        out: list[Assessment] = []
        for raw in names:
            target = raw.lower().strip()
            if not target:
                continue
            # Tier 1: exact (case-insensitive)
            hit = idx_by_name.get(target)
            if hit is not None:
                out.append(hit)
                continue
            if not fuzzy:
                continue
            # Tier 2: normalized exact
            target_norm = _normalize_name(raw)
            hit = idx_by_norm.get(target_norm)
            if hit is not None:
                out.append(hit)
                continue
            # Tier 3: substring on normalized form (handles unicode dashes etc.)
            for norm, a in idx_by_norm.items():
                if target_norm and (target_norm in norm or norm in target_norm):
                    hit = a
                    break
            if hit is not None:
                out.append(hit)
                continue
            # Tier 4: token overlap. Pick the assessment whose tokens cover
            # the most of the target's tokens (>= 50%).
            target_tokens = set(target_norm.split())
            if not target_tokens:
                continue
            best: tuple[float, Assessment] | None = None
            for tokens, a in norm_tokens:
                if not tokens:
                    continue
                overlap = len(target_tokens & tokens) / max(1, len(target_tokens))
                if overlap >= 0.5 and (best is None or overlap > best[0]):
                    best = (overlap, a)
            if best is not None:
                out.append(best[1])
        return out

    @property
    def catalog(self) -> list[Assessment]:
        self.load()
        return self._catalog


# Unicode dash variants we intentionally normalize to ASCII space:
# EN DASH, EM DASH, MINUS, HYPHEN, NON-BREAKING HYPHEN, HYPHEN-MINUS.
# The ambiguity is the WHOLE POINT of this constant, so we silence RUF001.
_DASH_CHARS = "–—−‐‑-"  # noqa: RUF001
_TRANSLATE_DASH = str.maketrans({c: " " for c in _DASH_CHARS})
_PUNCT_TO_SPACE = str.maketrans({c: " " for c in '().,/[]{}|:;"\''})


def _normalize_name(name: str) -> str:
    """Normalize an assessment name for fuzzy matching.

    - lowercase
    - replace unicode/ASCII dashes with spaces
    - strip basic punctuation
    - collapse whitespace
    """
    n = name.lower().translate(_TRANSLATE_DASH).translate(_PUNCT_TO_SPACE)
    return " ".join(n.split())


def _passes(a: Assessment, f: RetrievalFilters) -> bool:
    if f.test_types and not (set(a.test_types) & f.test_types):
        return False
    if f.require_remote_testing is True and not a.remote_testing:
        return False
    return not (f.require_adaptive is True and not a.adaptive_irt)


def _cache_key(
    query: str,
    filters: RetrievalFilters | None,
    top_k: int,
) -> tuple:
    """Build a hashable cache key from search inputs."""
    f = filters or RetrievalFilters()
    return (
        query.strip().lower(),
        tuple(sorted(f.test_types)),
        f.require_remote_testing,
        f.require_adaptive,
        top_k,
    )
