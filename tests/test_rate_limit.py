"""Token-bucket rate limiter tests."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.rate_limit import RateLimitMiddleware, TokenBucketLimiter


def test_token_bucket_allows_burst_then_throttles() -> None:
    b = TokenBucketLimiter(capacity=3, refill_per_sec=0.5)
    # Three immediate consumes should pass.
    for _ in range(3):
        ok, _ = b.consume("ip1")
        assert ok
    # Fourth consume blocked.
    ok, retry = b.consume("ip1")
    assert not ok
    assert retry > 0


def test_token_bucket_per_key_isolation() -> None:
    b = TokenBucketLimiter(capacity=1, refill_per_sec=10)
    ok, _ = b.consume("ip1")
    assert ok
    ok2, _ = b.consume("ip2")  # different key, fresh bucket
    assert ok2


def test_token_bucket_refills_over_time() -> None:
    b = TokenBucketLimiter(capacity=1, refill_per_sec=100)
    assert b.consume("ip")[0]
    assert not b.consume("ip")[0]
    time.sleep(0.05)
    # 100 tps * 50ms = 5 tokens available, but capacity caps at 1.
    assert b.consume("ip")[0]


def test_evict_idle_removes_old_keys() -> None:
    b = TokenBucketLimiter(capacity=1, refill_per_sec=1)
    b.consume("old")
    # Force the last-touched timestamp into the past.
    b._buckets["old"].last -= 10000  # type: ignore[attr-defined]
    removed = b.evict_idle(max_age_seconds=600)
    assert removed == 1


def test_middleware_429_payload_matches_spec_shape() -> None:
    app = FastAPI()

    @app.post("/chat")
    async def echo(_body: dict) -> dict:
        return {"reply": "ok", "recommendations": [], "end_of_conversation": False}

    app.add_middleware(
        RateLimitMiddleware,
        limiter=TokenBucketLimiter(capacity=1, refill_per_sec=0.1),
    )
    client = TestClient(app)
    r1 = client.post("/chat", json={"messages": []})
    assert r1.status_code == 200
    r2 = client.post("/chat", json={"messages": []})
    assert r2.status_code == 429
    body = r2.json()
    assert "reply" in body and "recommendations" in body and "end_of_conversation" in body
    assert body["recommendations"] == []
    assert "retry-after" in r2.headers


def test_middleware_skips_health() -> None:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    app.add_middleware(
        RateLimitMiddleware,
        limiter=TokenBucketLimiter(capacity=1, refill_per_sec=0.0),
    )
    client = TestClient(app)
    # Capacity is 1, refill is 0 — should hit limit fast on a normal path,
    # but /health is in the skip list and never throttles.
    for _ in range(5):
        assert client.get("/health").status_code == 200
