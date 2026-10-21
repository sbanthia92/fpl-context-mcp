"""
Small in-memory rate limiter for the HTTP transport.

A fixed-window counter per key (bearer token or client IP). State is per process, so
with several replicas each enforces its own limit; that is acceptable for abuse
protection, which is all this is for.
"""

import time


class RateLimiter:
    """Allow at most ``limit`` calls per ``window`` seconds for each key."""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._counts: dict[str, tuple[float, int]] = {}  # key -> (window start, count)

    def check(self, key: str) -> float:
        """
        Record a call for ``key``.

        Returns:
            0.0 when allowed, otherwise the seconds until the window resets.
        """
        if self.limit <= 0:
            return 0.0
        now = time.monotonic()
        start, count = self._counts.get(key, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        if count >= self.limit:
            return max(self.window - (now - start), 0.001)
        if key not in self._counts and len(self._counts) >= self.max_keys:
            self._evict(now)
        self._counts[key] = (start, count + 1)
        return 0.0

    def _evict(self, now: float) -> None:
        expired = [k for k, (start, _) in self._counts.items() if now - start >= self.window]
        for k in expired:
            del self._counts[k]
        if len(self._counts) >= self.max_keys:  # all live: drop the oldest window
            del self._counts[min(self._counts, key=lambda k: self._counts[k][0])]
