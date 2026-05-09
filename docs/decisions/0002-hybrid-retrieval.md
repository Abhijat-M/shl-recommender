# ADR 0002: Hybrid retrieval — FAISS dense + BM25 sparse + RRF fusion

* **Status:** Accepted
* **Date:** 2026-05-09

## Context

The agent must retrieve relevant assessments from ~377 catalog items given
a free-text query like "Hiring a Java developer who works with stakeholders".
Two failure modes must be avoided:

1. **Vocabulary mismatch:** the user says "Java developer" but the catalog
   has "Java 8 (New)" and "Core Java (Entry Level)". Pure keyword matching
   fails on synonyms / paraphrase.
2. **Exact-term loss:** the user names a specific product ("OPQ32r",
   "Verify Numerical Reasoning"). Pure embeddings can blur the distinction
   between OPQ32r and other personality questionnaires.

## Decision

Combine two retrieval methods and fuse the rankings:

* **Dense:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim) into a FAISS
  `IndexFlatIP` (cosine similarity via L2-normalization). Top-25.
* **Sparse:** `rank_bm25.BM25Okapi` over an alphanumeric-tokenized corpus.
  Top-25.
* **Fusion:** Reciprocal Rank Fusion (RRF) with k=60.
  `score = Σ_i 1 / (k + rank_i)`.

Apply soft filters (test-type, remote-testing) **after** fusion. If filters
drop result count < 3, retry without the test-type filter.

## Alternatives considered

1. **Dense only.** Recall@10 was acceptable on broad queries but missed
   exact product names like "OPQ32r" by 1-2 ranks.
2. **BM25 only.** Failed on paraphrase queries ("hiring someone for sales"
   → didn't surface "Sales Transformation Report").
3. **Dense + sparse with normalized score addition** (e.g. min-max). RRF
   is score-free and immune to magnitude differences, which mattered here
   because BM25 scores swing wildly across queries.
4. **A reranker (e.g. `bge-reranker-base`).** Rejected: adds ~300 ms per
   query and another ~400 MB of model weights, blowing the free-tier image
   size and cold-start budget. The LLM generator already does an implicit
   rerank by picking indices.
5. **Vector DB service (Pinecone, Weaviate, pgvector).** Rejected: the
   catalog is small (377 items) — exact FAISS search is sub-millisecond.
   External services add latency, cost, and another failure point.

## Consequences

**Positive:**
- Strong recall on both paraphrase and exact-name queries. Internal probes
  show Recall@10 lifted from ~0.55 (dense-only with strict filters) to
  ~0.85 with hybrid + soft-fallback filters.
- No external retrieval service to pay for or operate.
- Deterministic ranking (same query → same result, modulo cache).
- ~1 ms FAISS + ~5 ms BM25 per query; fusion is `O(K)`.

**Negative:**
- Two artifacts to keep in sync (FAISS index + BM25 pickle). Both are
  rebuilt from the catalog by `scripts/build_index.py`.
- BM25 corpus must be re-tokenized at build time; stop-word policy is
  baked in (we use the rank_bm25 default — alphanumeric word tokens).
