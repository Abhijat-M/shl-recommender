"""Simple in-memory token-bucket rate limiter.

For a single-instance free-tier deployment, in-memory is sufficient and avoids
adding Redis as a dependency. If you scale horizontally, swap this for a
Redis-backed limiter (see docs/operations.md).

Algorithm: per-client-IP token bucket.
  - capacity:  maximum burst (default 10)
  - refill:    tokens added per second (default 1)

Each request consumes 1 token. If tokens < 1, return 429 with
`Retry-After`. The bucket is keyed by `X-Forwarded-For` first (if behind a
proxy, e.g. Render), falling back to the socket peer. We trust the first
forwarded IP only — the entire chain is not used as a key.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import ClassVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

NextHandler = Callable[[Request], Awaitable[Response]]


@dataclass
class _Bucket:
    tokens: float
    last: float


class TokenBucketLimiter:
    """Threadsafe per-key token bucket."""

    def __init__(self, capacity: float = 10.0, refill_per_sec: float = 1.0) -> None:
        self.capacity = capacity
        self.refill = refill_per_sec
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def consume(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Try to consume `cost` tokens. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=self.capacity, last=now)
                self._buckets[key] = b
            # Refill
            elapsed = now - b.last
            b.tokens = min(self.capacity, b.tokens + elapsed * self.refill)
            b.last = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0
            shortfall = cost - b.tokens
            retry_after = shortfall / self.refill if self.refill > 0 else 60.0
            return False, retry_after

    def evict_idle(self, max_age_seconds: float = 600.0) -> int:
        """Garbage-collect buckets idle for longer than `max_age_seconds`."""
        cutoff = time.monotonic() - max_age_seconds
        with self._lock:
            stale = [k for k, b in self._buckets.items() if b.last < cutoff]
            for k in stale:
                del self._buckets[k]
            return len(stale)


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply a `TokenBucketLimiter` to /chat. Skips /health and /ready.

    Returns 429 with a JSON body matching the spec's empty-recs convention so
    automated callers don't choke on a different shape.
    """

    SKIP_PATHS: ClassVar[set[str]] = {"/health", "/ready", "/version", "/metrics"}

    def __init__(
        self,
        app: ASGIApp,
        limiter: TokenBucketLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter or TokenBucketLimiter()

    async def dispatch(self, request: Request, call_next: NextHandler) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)
        key = _client_key(request)
        allowed, retry_after = self._limiter.consume(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "reply": (
                        "Too many requests in a short window. Please retry shortly."
                    ),
                    "recommendations": [],
                    "end_of_conversation": False,
                },
                headers={"retry-after": str(int(retry_after) + 1)},
            )
        return await call_next(request)
