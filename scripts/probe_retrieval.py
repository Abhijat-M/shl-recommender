"""Smoke-test the hybrid retriever against a few representative queries."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.retrieval.retriever import HybridRetriever  # noqa: E402

QUERIES = [
    "Hiring a Java developer who works with stakeholders, mid-level",
    "Need a personality test for sales role",
    "Compare OPQ32r and Verify Numerical Reasoning",
    "Python coding test for software engineer",
    "Cognitive ability test for graduates under 30 minutes",
    "Customer service simulation for call center",
]


def main() -> int:
    r = HybridRetriever().load()
    for q in QUERIES:
        print(f"\n>>> {q}")
        hits = r.search(q, top_k=5)
        for h in hits:
            a = h.assessment
            print(
                f"  [{h.score:.4f}]  {a.name[:60]:60} types={a.test_types}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
