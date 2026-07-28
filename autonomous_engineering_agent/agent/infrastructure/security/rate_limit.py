"""In-memory fixed-window rate limiter for a single dashboard process.

Not distributed: each process replica tracks its own counters. That is an
acceptable trade-off for a small beta launch behind one or two dashboard
instances; a shared limiter (Redis, Postgres) is unnecessary complexity here.
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        cutoff = current - self._window_seconds
        hits = self._hits.setdefault(key, [])
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self._max_requests:
            return False
        hits.append(current)
        return True
