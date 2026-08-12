"""Rate limiting without an external cache.

ElastiCache/Redis are prohibited for version 1, so limiting is enforced in two
independent places:

1. Caddy applies coarse per-IP connection and request limits at the edge.
2. This module applies a per-process sliding-window limiter keyed by user,
   device and client IP.

Because the API runs `API_WORKERS` Gunicorn workers, the effective per-key
budget is `RATE_LIMIT_REQUESTS_PER_MINUTE` divided by the worker count so that
the fleet-wide limit matches the configured value regardless of which
worker handles a request. This is documented in docs/SECURITY.md.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 60.0
MAX_TRACKED_KEYS = 100_000
_CLEANUP_EVERY = 2048


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after: int
    limit: int


class SlidingWindowLimiter:
    """Thread-safe sliding-window counter with bounded memory."""

    def __init__(self, *, limit: int, window_seconds: float = WINDOW_SECONDS) -> None:
        self.limit = max(1, limit)
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._ops = 0

    def _prune(self, now: float) -> None:
        stale = [
            key for key, hits in self._hits.items() if not hits or now - hits[-1] > self.window
        ]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) > MAX_TRACKED_KEYS:
            # Drop the coldest half rather than growing without bound.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
            for key, _ in ordered[: len(ordered) // 2]:
                self._hits.pop(key, None)

    def check(self, key: str, *, cost: int = 1, now: float | None = None) -> RateLimitDecision:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._ops += 1
            if self._ops % _CLEANUP_EVERY == 0:
                self._prune(now)

            hits = self._hits.setdefault(key, deque())
            cutoff = now - self.window
            while hits and hits[0] < cutoff:
                hits.popleft()

            if len(hits) + cost > self.limit:
                oldest = hits[0] if hits else now
                retry_after = max(1, int(self.window - (now - oldest)) + 1)
                return RateLimitDecision(False, 0, retry_after, self.limit)

            for _ in range(cost):
                hits.append(now)
            return RateLimitDecision(True, self.limit - len(hits), 0, self.limit)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._ops = 0


class CompositeRateLimiter:
    """Applies user, device and IP limiters; the strictest verdict wins."""

    def __init__(self, *, per_minute: int, workers: int = 1, burst: int = 60) -> None:
        effective = max(1, per_minute // max(1, workers))
        self.user = SlidingWindowLimiter(limit=effective)
        self.device = SlidingWindowLimiter(limit=effective)
        # IP limit is looser: many employees may share one NAT egress.
        self.ip = SlidingWindowLimiter(limit=max(effective, effective * 4))
        self.burst = SlidingWindowLimiter(
            limit=max(1, burst // max(1, workers)), window_seconds=1.0
        )

    def check(
        self,
        *,
        user_key: str | None,
        device_key: str | None,
        ip_key: str | None,
        cost: int = 1,
    ) -> RateLimitDecision:
        checks: list[RateLimitDecision] = []
        if user_key:
            checks.append(self.user.check(f"u:{user_key}", cost=cost))
            checks.append(self.burst.check(f"bu:{user_key}", cost=cost))
        if device_key:
            checks.append(self.device.check(f"d:{device_key}", cost=cost))
        if ip_key:
            checks.append(self.ip.check(f"i:{ip_key}", cost=cost))
        if not checks:
            return RateLimitDecision(True, 0, 0, 0)

        denied = [c for c in checks if not c.allowed]
        if denied:
            worst = max(denied, key=lambda c: c.retry_after)
            return worst
        return min(checks, key=lambda c: c.remaining)

    def reset(self) -> None:
        for limiter in (self.user, self.device, self.ip, self.burst):
            limiter.reset()
