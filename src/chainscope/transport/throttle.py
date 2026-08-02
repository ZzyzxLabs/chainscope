"""Per-host rate limiting.

Two reasons this is on by default rather than opt-in.

The obvious one: most of the data sources this tool uses are free, and hammering
them is both rude and self-defeating --- you get rate-limited, then banned, then
your investigation stalls.

The less obvious one: several research and competition contexts explicitly
prohibit excessive automated requests, and "I did not know my script was doing
that" has never been a good answer. A default that behaves well is worth more
than a warning in a README.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = ["Throttle", "TokenBucket"]


@dataclass
class TokenBucket:
    """Classic token bucket: sustained ``rate``, burst up to ``capacity``.

    Bursting is intentional. A cold cache legitimately needs a handful of
    requests at once; a flat per-request delay would make that needlessly slow
    without protecting anyone.
    """

    rate: float
    capacity: float
    _tokens: float = 0.0
    _last: float = 0.0

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be positive")
        self.capacity = max(self.capacity, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()

    def take(self, n: float = 1.0) -> float:
        """Consume ``n`` tokens, returning how long the caller should wait first."""
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return 0.0
        deficit = n - self._tokens
        self._tokens = 0.0
        return deficit / self.rate


class Throttle:
    """Rate limits keyed by host.

    Per-host rather than global: one slow explorer should not stall queries to
    an unrelated RPC endpoint.
    """

    def __init__(
        self,
        default_rate: float = 5.0,
        default_burst: float = 10.0,
        overrides: dict[str, float] | None = None,
    ) -> None:
        self.default_rate = default_rate
        self.default_burst = default_burst
        self.overrides = overrides or {}
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    @staticmethod
    def host_of(url: str) -> str:
        return urlparse(url).netloc or url

    def _bucket(self, host: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                rate = self.overrides.get(host, self.default_rate)
                # Burst has to scale *down* with the rate. `max(rate * 2,
                # default_burst)` made the burst a floor, so lowering the rate
                # to respect a strict API still produced a ten-request burst --
                # and a bucket that starts full spends it immediately, which is
                # exactly when a sweep or a recording run makes its first calls.
                # Capping at one second's allowance makes a configured rate a
                # promise the caller can rely on.
                capacity = min(max(rate, 1.0), self.default_burst)
                bucket = TokenBucket(rate=rate, capacity=capacity)
                self._buckets[host] = bucket
            return bucket

    def acquire(self, url: str) -> float:
        """Block until a request to ``url`` is permitted. Returns seconds waited."""
        host = self.host_of(url)
        # _bucket() takes the lock itself; threading.Lock is not reentrant, so
        # this must not be called while holding it.
        bucket = self._bucket(host)
        with self._lock:
            wait = bucket.take()
        if wait > 0:
            time.sleep(wait)
        return wait

    def set_rate(self, host: str, rate: float) -> None:
        """Adjust a host's rate, e.g. after a 429 or an operator's guidance."""
        with self._lock:
            self.overrides[host] = rate
            self._buckets.pop(host, None)

    def stats(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                host: {"rate": b.rate, "tokens": round(b._tokens, 2)}
                for host, b in self._buckets.items()
            }
