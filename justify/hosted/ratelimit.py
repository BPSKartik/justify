"""Per-visitor rate limit: a token bucket per client address, refilled continuously."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, per_hour: int):
        self.capacity = max(1, per_hour)
        self.rate = self.capacity / 3600.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def take(self, key: str) -> tuple[bool, int]:
        """(allowed, seconds until the next one would be allowed)."""
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, now)
                ok, wait = True, 0
            else:
                self.buckets[key] = (tokens, now)
                ok, wait = False, int((1 - tokens) / self.rate) + 1
            if len(self.buckets) > 50_000:          # forget visitors whose bucket is full again
                full = [k for k, (t, ts) in self.buckets.items() if t + (now - ts) * self.rate >= self.capacity]
                for k in full:
                    del self.buckets[k]
        return ok, wait


def client_ip(headers, peer: str | None, trusted_hops: int = 1) -> str:
    """The visitor's address. Behind Azure's ingress the last X-Forwarded-For entry is the one
    the proxy added; anything before it was written by the client and can be forged."""
    xff = headers.get("x-forwarded-for") if headers else None
    if xff and trusted_hops > 0:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-min(trusted_hops, len(parts))]
    return peer or "unknown"
