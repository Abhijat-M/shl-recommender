"""Catalog domain model + JSON I/O.

The catalog is a flat list of `Assessment` records. We keep it as a single
JSON file so the scraper, indexer, retriever, and tests share one schema.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

# SHL's test type taxonomy (from the catalog legend)
TEST_TYPE_NAMES: dict[str, str] = {
    "A": "Ability & Aptitude",
    "B": "Biodata & Situational Judgement",
    "C": "Competencies",
    "D": "Development & 360",
    "E": "Assessment Exercises",
    "K": "Knowledge & Skills",
    "P": "Personality & Behavior",
    "S": "Simulations",
}


@dataclass(slots=True)
class Assessment:
    """Single Individual Test Solution.

    `name` and `url` are the only fields the API returns. `test_types` is a list
    so we can return any one of the codes (the API schema takes a single
    `test_type` string; we'll join codes when returning).
    """

    # Identity (returned to API caller)
    name: str
    url: str
    test_types: list[str] = field(default_factory=list)

    # Catalog-row metadata (used for retrieval/filtering)
    remote_testing: bool = False
    adaptive_irt: bool = False

    # Scraped detail page (used for retrieval / compare)
    description: str = ""
    job_levels: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    assessment_length: str = ""

    @property
    def test_type_str(self) -> str:
        """Single string for the API field (joined codes)."""
        return "".join(self.test_types) if self.test_types else ""

    @property
    def test_type_full_names(self) -> list[str]:
        return [TEST_TYPE_NAMES.get(c, c) for c in self.test_types]

    def search_text(self) -> str:
        """Concatenated text used as the retrieval document."""
        parts: list[str] = [self.name]
        if self.test_types:
            parts.append(
                "Test types: " + ", ".join(self.test_type_full_names)
            )
        if self.job_levels:
            parts.append("Job levels: " + ", ".join(self.job_levels))
        if self.languages:
            parts.append("Languages: " + ", ".join(self.languages))
        if self.assessment_length:
            parts.append(f"Assessment length: {self.assessment_length}")
        if self.description:
            parts.append(self.description)
        return "\n".join(parts)


def save_catalog(assessments: Iterable[Assessment], path: str | Path) -> None:
    """Persist the catalog to JSON. Atomic write via tmp file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(a) for a in assessments]
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_catalog(path: str | Path) -> list[Assessment]:
    """Load the catalog from JSON. Returns [] if the file is missing."""
    p = Path(path)
    if not p.exists():
        return []
    payload = json.loads(p.read_text(encoding="utf-8"))
    out: list[Assessment] = []
    for row in payload:
        # Tolerant load: ignore unexpected fields, default missing.
        out.append(
            Assessment(
                name=row.get("name", ""),
                url=row.get("url", ""),
                test_types=list(row.get("test_types", [])),
                remote_testing=bool(row.get("remote_testing", False)),
                adaptive_irt=bool(row.get("adaptive_irt", False)),
                description=row.get("description", ""),
                job_levels=list(row.get("job_levels", [])),
                languages=list(row.get("languages", [])),
                assessment_length=row.get("assessment_length", ""),
            )
        )
    return out
