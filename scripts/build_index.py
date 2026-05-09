"""Build the FAISS+BM25 index from data/catalog/catalog.json.

Run after `scripts/scrape_catalog.py` produced the catalog file.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings
from src.retrieval.indexer import build


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(settings.catalog_file))
    parser.add_argument("--index-dir", default=str(settings.index_path))
    parser.add_argument("--model", default=settings.embedding_model)
    args = parser.parse_args()

    n = build(args.catalog, args.index_dir, args.model)
    print(f"Indexed {n} assessments into {args.index_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
