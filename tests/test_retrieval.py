"""Retrieval tests: hybrid search returns the right items for clear queries."""

from __future__ import annotations

from src.retrieval.retriever import HybridRetriever, RetrievalFilters


def test_java_query_returns_java_assessments(retriever: HybridRetriever) -> None:
    hits = retriever.search("Java developer entry level", top_k=3)
    names = [h.assessment.name.lower() for h in hits]
    assert any("java" in n for n in names), f"got {names}"


def test_python_query_returns_python(retriever: HybridRetriever) -> None:
    hits = retriever.search("Python coding skills", top_k=3)
    names = [h.assessment.name.lower() for h in hits]
    assert any("python" in n for n in names), f"got {names}"


def test_filter_by_test_type_personality(retriever: HybridRetriever) -> None:
    hits = retriever.search(
        "personality test for senior managers",
        filters=RetrievalFilters(test_types={"P"}),
        top_k=5,
    )
    assert hits, "expected at least one hit"
    for h in hits:
        assert "P" in h.assessment.test_types


def test_lookup_by_name_exact(retriever: HybridRetriever) -> None:
    found = retriever.lookup_by_names(["Java 8 (New)"])
    assert len(found) == 1
    assert found[0].name == "Java 8 (New)"


def test_lookup_by_name_fuzzy(retriever: HybridRetriever) -> None:
    found = retriever.lookup_by_names(["OPQ32r"], fuzzy=True)
    assert any("OPQ" in a.name for a in found)


def test_lookup_handles_unicode_dash(retriever: HybridRetriever) -> None:
    """User typing 'Verify Interactive Numerical Reasoning' (ASCII hyphen or none)
    should match the catalog entry which has a unicode en-dash."""
    # User uses an ASCII hyphen
    found = retriever.lookup_by_names(
        ["SHL Verify Interactive - Numerical Reasoning"], fuzzy=True
    )
    assert any("Numerical Reasoning" in a.name for a in found)
    # User omits the dash entirely
    found = retriever.lookup_by_names(
        ["SHL Verify Interactive Numerical Reasoning"], fuzzy=True
    )
    assert any("Numerical Reasoning" in a.name for a in found)


def test_lookup_token_overlap_fallback(retriever: HybridRetriever) -> None:
    """A reasonable subset of tokens should still match."""
    found = retriever.lookup_by_names(["Verify Numerical Reasoning"], fuzzy=True)
    assert any("Numerical" in a.name for a in found)


def test_empty_query_returns_empty(retriever: HybridRetriever) -> None:
    assert retriever.search("") == []
    assert retriever.search("   ") == []
