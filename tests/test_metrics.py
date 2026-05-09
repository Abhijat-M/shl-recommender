"""Metrics registry + Prometheus exposition format tests."""

from __future__ import annotations

import re

from src.metrics import Metrics


def test_counter_with_labels_renders() -> None:
    m = Metrics()
    m.inc("shl_test_counter", labels={"intent": "search"})
    m.inc("shl_test_counter", labels={"intent": "search"})
    m.inc("shl_test_counter", labels={"intent": "clarify"})
    out = m.render_prometheus()
    assert "# TYPE shl_test_counter counter" in out
    assert 'shl_test_counter{intent="search"} 2' in out
    assert 'shl_test_counter{intent="clarify"} 1' in out


def test_counter_no_labels() -> None:
    m = Metrics()
    m.inc("shl_naked")
    m.inc("shl_naked", value=4)
    out = m.render_prometheus()
    assert "shl_naked 5" in out


def test_histogram_buckets_sum_count() -> None:
    m = Metrics()
    m.observe("shl_dur_ms", 30)     # falls in bucket 50
    m.observe("shl_dur_ms", 700)    # falls in bucket 1000
    m.observe("shl_dur_ms", 4000)   # falls in bucket 5000
    out = m.render_prometheus()
    assert "# TYPE shl_dur_ms histogram" in out
    assert re.search(r'shl_dur_ms_bucket{le="50"} 1', out)
    assert re.search(r'shl_dur_ms_bucket{le="\+Inf"} 3', out)
    assert "shl_dur_ms_count 3" in out
    assert "shl_dur_ms_sum 4730" in out


def test_label_value_escape() -> None:
    m = Metrics()
    m.inc("shl_x", labels={"k": 'a"b\\c'})
    out = m.render_prometheus()
    assert 'k="a\\"b\\\\c"' in out
