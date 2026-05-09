"""Simple thread-safe LRU cache keyed by retrieval query.

Why: the SHL evaluator runs many traces in parallel; common queries
("entry-level java", "personality test for sales") repeat across traces.
Caching the (filters, query) -> hits result trims tens of milliseconds and
removes pressure from the embedding model on free-tier CPUs.

This is intentionally minimal — we don't cache LLM calls (those depend on
conversation history) and we don't cache across processes (no Redis).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class LRUCache:
    """Threadsafe LRU. Generic over (key -> Any)."""

    def __init__(self, capacity: int = 128) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._data: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            if len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
