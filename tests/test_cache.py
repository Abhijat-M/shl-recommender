"""LRU cache tests."""

from __future__ import annotations

import pytest

from src.retrieval.cache import LRUCache


def test_basic_put_get() -> None:
    c = LRUCache(capacity=2)
    c.put("a", 1)
    assert c.get("a") == 1
    assert c.get("missing") is None


def test_lru_eviction() -> None:
    c = LRUCache(capacity=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)  # evicts "a"
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_lru_recency_protects_from_eviction() -> None:
    c = LRUCache(capacity=2)
    c.put("a", 1)
    c.put("b", 2)
    # Touch "a" so "b" becomes the LRU.
    c.get("a")
    c.put("c", 3)
    assert c.get("a") == 1
    assert c.get("b") is None


def test_hit_rate() -> None:
    c = LRUCache(capacity=2)
    assert c.hit_rate == 0.0
    c.put("a", 1)
    c.get("a")  # hit
    c.get("a")  # hit
    c.get("b")  # miss
    assert c.hits == 2 and c.misses == 1
    assert pytest.approx(c.hit_rate, rel=1e-3) == 2 / 3


def test_clear_resets_counters() -> None:
    c = LRUCache(capacity=2)
    c.put("a", 1)
    c.get("a")
    c.clear()
    assert len(c) == 0
    assert c.hits == 0 and c.misses == 0


def test_zero_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        LRUCache(capacity=0)
