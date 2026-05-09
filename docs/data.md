# Data Reference

The agent's source of truth is a **scraped snapshot of the SHL Individual
Test Solutions catalog** stored on disk. This doc describes the schema,
the SHL taxonomy, and how the catalog is built.

## Overview

```
shl.com (live HTML) ──▶ scrape_catalog.py ──▶ data/catalog/catalog.json
                                                       │
                                                       ▼
                                             build_index.py
                                                       │
                                                       ▼
                                       data/index/{dense.faiss, bm25.pkl,
                                                   documents.pkl, meta.pkl}
```

- `data/catalog/catalog.json` is **committed** to the repo. It is the
  authoritative version of the catalog at the last refresh.
- `data/index/` is **gitignored**. It's regenerated from `catalog.json`
  by `scripts/build_index.py` (~30 s) and is also pre-built into the
  Docker image.

## `Assessment` schema

Source: [`src/retrieval/catalog.py`](../src/retrieval/catalog.py).

```python
@dataclass(slots=True)
class Assessment:
    name: str                       # e.g. "Java 8 (New)"
    url: str                        # e.g. "https://www.shl.com/.../view/java-8-new/"
    test_types: list[str]           # SHL letter codes, e.g. ["K"]
    remote_testing: bool            # supports remote administration?
    adaptive_irt: bool              # adaptive / Item Response Theory?
    description: str                # 1-3 paragraphs from the detail page
    job_levels: list[str]           # e.g. ["Mid-Professional", "Manager"]
    languages: list[str]            # e.g. ["English (USA)", "French"]
    assessment_length: str          # e.g. "Approximate Completion Time in minutes = 25"
```

Catalog stats (latest scrape, 377 entries):

| Field | Coverage |
|-------|---------:|
| `name`, `url`, `test_types` | 377 / 377 |
| `description` | 377 / 377 |
| `assessment_length` | 316 / 377 |
| `job_levels` | 361 / 377 |
| `languages` | 341 / 377 |
| `remote_testing == True` | 377 / 377 (all Individual Test Solutions support remote) |
| `adaptive_irt == True` | 37 / 377 |

The retrieval document (`Assessment.search_text()`) concatenates name,
test type names, job levels, languages, length, and description into a
single string for both FAISS embedding and BM25 indexing.

## SHL test-type taxonomy

The eight letter codes from the SHL catalog legend:

| Code | Name | Typical fit |
|------|------|-------------|
| **A** | Ability & Aptitude | Numerical / verbal / inductive reasoning |
| **B** | Biodata & Situational Judgement | Past-experience and scenario tests |
| **C** | Competencies | Manager / leadership skill batteries |
| **D** | Development & 360 | Self & peer feedback for dev plans |
| **E** | Assessment Exercises | In-tray, role-plays, group exercises |
| **K** | Knowledge & Skills | Tech / domain knowledge multi-choice (the largest bucket: 240 items) |
| **P** | Personality & Behavior | OPQ32r and friends |
| **S** | Simulations | Job-task simulations (call-center, coding) |

A single assessment can have multiple codes; the API returns them
joined as one string (e.g. `"KP"` for Knowledge + Personality).

Distribution across the 377-item catalog:

| Code | Count |
|------|------:|
| K | 240 |
| P | 66 |
| S | 43 |
| A | 32 |
| C | 19 |
| B | 17 |
| D | 7 |
| E | 2 |

## How the catalog is scraped

`scripts/scrape_catalog.py`:

1. Walks `https://www.shl.com/products/product-catalog/?start=N&type=1`
   in 12-row pages.
2. On each page, identifies the **Individual Test Solutions** table by
   its `<th class="custom__table-heading__title">Individual Test
   Solutions</th>` heading (the page also contains a Pre-packaged Job
   Solutions table; we skip that).
3. Per row, reads:
   - The product name + relative URL from the first cell's `<a>` tag.
   - Remote testing flag from the `<span class="catalogue__circle -yes">`
     marker in the second cell.
   - Adaptive/IRT flag from the same marker in the third cell.
   - Test-type letters from `<span class="product-catalogue__key">`
     spans in the fourth cell.
4. For every detail URL, fetches the page and extracts:
   - Description (first long `<p>` after `<h1>` if no labeled section).
   - Job levels, languages, assessment length (labeled paragraphs).
5. Writes `data/catalog/catalog.json` atomically (tmp + rename).

The scraper is **polite**: 0.3 s delay between listing pages and a
6-connection concurrency cap on detail pages.

### When to re-scrape

The SHL catalog changes occasionally (new assessments, retired ones, name
revisions). Recommended cadence:

- Manual refresh before any submission to SHL.
- Quarterly otherwise.

Refresh procedure:

```bash
python scripts/scrape_catalog.py
python scripts/verify_catalog.py        # spot-check the new data
python scripts/build_index.py           # rebuild FAISS + BM25
git add data/catalog/catalog.json
git commit -m "chore(catalog): refresh from shl.com"
git push
```

The next deploy ships the new catalog. The image rebuilds the index
from `catalog.json` automatically (multi-stage Dockerfile).

## How the index is built

`scripts/build_index.py` writes four artifacts to `data/index/`:

| File | Purpose |
|------|---------|
| `dense.faiss` | FAISS `IndexFlatIP` over L2-normalized MiniLM embeddings (384-dim, via fastembed/ONNX) |
| `embeddings.npy` | Raw normalized embedding matrix (used for warm-restarts of indexing experiments) |
| `bm25.pkl` | `rank_bm25.BM25Okapi` instance + the tokenized corpus |
| `documents.pkl` | The `search_text()` string per assessment (in catalog order) |
| `meta.pkl` | `{model_name, catalog (full Assessment list), n}` — the canonical join key |

The retriever loads all five at startup and builds an in-memory hybrid
index. Search latency is ~6 ms per query (1 ms FAISS + 5 ms BM25 + RRF
fusion).

## Catalog-only guarantee

The agent **cannot** return an assessment that isn't in `catalog.json`:

1. The router/recommend prompts force the LLM to pick **integer indices**
   into the candidate list, not free-form names. (See ADR-0003.)
2. The candidate list is built from the in-memory `meta.catalog` (which
   was loaded from `catalog.json` at startup).
3. Every emitted recommendation's URL is filtered against the URL set of
   `meta.catalog` before serialization — anything else is dropped.

Refreshing the catalog is therefore the **only** way to change what the
agent can recommend. There is no runtime "search the live web" path.
