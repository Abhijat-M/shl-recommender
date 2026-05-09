"""Tiny in-process metrics, exported in Prometheus text format.

We don't pull in the `prometheus_client` package because the metric set is
small, and depending on it complicates the free-tier image. The format is
documented and stable, so a hand-rolled exporter is fine here.

Counters and histograms:
  shl_chat_requests_total{status="ok"}        # successful turns
  shl_chat_requests_total{status="error"}     # exceptions
  shl_chat_intent_total{intent="search"}      # routing decisions
  shl_chat_recs_returned_total                # cumulative recs emitted
  shl_chat_duration_ms_bucket{le="..."}       # histogram of /chat latency
  shl_llm_calls_total{kind="json|text"}       # LLM round-trips
  shl_llm_errors_total                        # failed LLM rounds (after retry)
  shl_retrieval_calls_total                   # hybrid search invocations

If you outgrow this, swap to `prometheus_client` (compatible scrape format).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

# Default histogram buckets in milliseconds; cover sub-100ms (cache hits) up to
# 30 seconds (the spec's per-call timeout).
_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    25, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000, 30000,
)


@dataclass
class _HistogramState:
    bucket_counts: list[int] = field(
        default_factory=lambda: [0] * (len(_LATENCY_BUCKETS_MS) + 1)
    )
    sum_ms: float = 0.0
    count: int = 0


class Metrics:
    """Threadsafe metrics registry. Singleton via `get_metrics`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _HistogramState] = (
            defaultdict(_HistogramState)
        )

    # ----- counters -----
    def inc(self, name: str, value: int = 1, labels: dict[str, str] | None = None) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            self._counters[key] += value

    # ----- histograms -----
    def observe(
        self,
        name: str,
        value_ms: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            h = self._histograms[key]
            h.sum_ms += value_ms
            h.count += 1
            for i, b in enumerate(_LATENCY_BUCKETS_MS):
                if value_ms <= b:
                    h.bucket_counts[i] += 1
            # +Inf bucket (always increments)
            h.bucket_counts[-1] += 1

    # ----- export -----
    def render_prometheus(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        with self._lock:
            counters = dict(self._counters)
            histograms = {k: _HistogramState(
                bucket_counts=list(v.bucket_counts),
                sum_ms=v.sum_ms,
                count=v.count,
            ) for k, v in self._histograms.items()}

        lines: list[str] = []
        # counters
        seen_helps: set[str] = set()
        for (name, labels), val in sorted(counters.items()):
            if name not in seen_helps:
                lines.append(f"# TYPE {name} counter")
                seen_helps.add(name)
            lines.append(f"{name}{_label_str(labels)} {val}")
        # histograms
        seen_helps = set()
        for (name, labels), h in sorted(histograms.items()):
            if name not in seen_helps:
                lines.append(f"# TYPE {name} histogram")
                seen_helps.add(name)
            cumulative = 0
            for i, b in enumerate(_LATENCY_BUCKETS_MS):
                cumulative += h.bucket_counts[i]
                lines.append(
                    f'{name}_bucket{_label_str(labels, extra=("le", str(b)))} '
                    f"{cumulative}"
                )
            lines.append(
                f'{name}_bucket{_label_str(labels, extra=("le", "+Inf"))} '
                f"{h.count}"
            )
            lines.append(f"{name}_sum{_label_str(labels)} {h.sum_ms:.3f}")
            lines.append(f"{name}_count{_label_str(labels)} {h.count}")
        return "\n".join(lines) + "\n"


def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _label_str(
    labels: Iterable[tuple[str, str]],
    *,
    extra: tuple[str, str] | None = None,
) -> str:
    items = list(labels)
    if extra is not None:
        items.append(extra)
    if not items:
        return ""
    rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in items)
    return "{" + rendered + "}"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


_singleton: Metrics | None = None
_singleton_lock = threading.Lock()


def get_metrics() -> Metrics:
    """Return the process-wide metrics registry (lazy singleton)."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = Metrics()
    return _singleton
