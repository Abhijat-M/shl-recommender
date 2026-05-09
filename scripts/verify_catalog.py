"""Print quality stats and a few sample rows from the scraped catalog."""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        cat = json.load(f)
    n = len(cat)
    print(f"Total: {n}")
    print(f"Has description: {sum(1 for a in cat if a['description'])}/{n}")
    print(f"Has assessment_length: {sum(1 for a in cat if a['assessment_length'])}/{n}")
    print(f"Has job_levels: {sum(1 for a in cat if a['job_levels'])}/{n}")
    print(f"Has languages: {sum(1 for a in cat if a['languages'])}/{n}")
    print(f"Has test_types: {sum(1 for a in cat if a['test_types'])}/{n}")
    print(f"Remote=true: {sum(1 for a in cat if a['remote_testing'])}/{n}")
    print(f"Adaptive=true: {sum(1 for a in cat if a['adaptive_irt'])}/{n}")

    print("\nTest type distribution:")
    c: Counter[str] = Counter()
    for a in cat:
        for t in a["test_types"]:
            c[t] += 1
    for t, k in sorted(c.items()):
        print(f"  {t}: {k}")

    print("\nSample 5 entries:")
    random.seed(42)
    for a in random.sample(cat, min(5, n)):
        print(f"  - {a['name']!r}")
        print(f"      types={a['test_types']} remote={a['remote_testing']} adaptive={a['adaptive_irt']}")
        print(f"      length={a['assessment_length']!r}")
        print(f"      languages={a['languages'][:6]}")
        print(f"      job_levels={a['job_levels'][:6]}")
        desc = a["description"][:160]
        print(f"      desc: {desc}")
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / "data" / "catalog" / "catalog.json")
    raise SystemExit(main(p))
