"""Rate limiting.

Includes a regression test for a deadlock: ``acquire`` held the throttle lock
while calling ``_bucket``, which takes the same lock. ``threading.Lock`` is not
reentrant, so the second acquisition blocked forever --- and single-threaded
tests never noticed, because the first caller through was already inside.
"""

import threading
import time

from chainscope.transport.throttle import Throttle, TokenBucket


class TestTokenBucket:
    def test_burst_is_allowed_up_to_capacity(self):
        # A cold cache legitimately needs several requests at once; a flat
        # per-request delay would punish that for no benefit to anyone.
        b = TokenBucket(rate=5, capacity=5)
        assert all(b.take() == 0.0 for _ in range(5))

    def test_exhausted_bucket_reports_a_wait(self):
        b = TokenBucket(rate=10, capacity=1)
        b.take()
        wait = b.take()
        assert 0 < wait <= 0.2

    def test_tokens_refill_over_time(self):
        b = TokenBucket(rate=100, capacity=1)
        b.take()
        time.sleep(0.05)
        assert b.take() == 0.0

    def test_rate_must_be_positive(self):
        import pytest

        with pytest.raises(ValueError, match="positive"):
            TokenBucket(rate=0, capacity=1)


class TestThrottle:
    def test_hosts_are_limited_independently(self):
        """One slow explorer must not stall an unrelated RPC endpoint."""
        t = Throttle(default_rate=1, default_burst=1)
        assert t.acquire("https://a.example/x") == 0.0
        assert t.acquire("https://b.example/y") == 0.0

    def test_same_host_shares_a_bucket(self):
        # Capacity is max(rate * 2, default_burst) -- always at least two
        # seconds of headroom, so a low rate is needed to observe queueing.
        t = Throttle(default_rate=1, default_burst=1)
        for _ in range(2):
            assert t.acquire("https://a.example/x") == 0.0
        assert t.acquire("https://a.example/y") > 0

    def test_capacity_is_at_least_two_seconds_of_rate(self):
        """Documents the formula, which is easy to misread as 'burst == capacity'."""
        t = Throttle(default_rate=50, default_burst=1)
        t.acquire("https://a.example/x")
        assert t.stats()["a.example"]["rate"] == 50

    def test_set_rate_takes_effect(self):
        t = Throttle(default_rate=100)
        t.set_rate("a.example", 1.0)
        t.acquire("https://a.example/x")
        assert t.stats()["a.example"]["rate"] == 1.0

    def test_concurrent_acquire_does_not_deadlock(self):
        """Regression: acquire() called _bucket() while holding a non-reentrant
        lock, which blocked forever the moment two threads raced."""
        t = Throttle(default_rate=1000, default_burst=100)
        done = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(20):
                    t.acquire("https://a.example/x")
                    t.acquire("https://b.example/y")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()

        def joiner() -> None:
            for th in threads:
                th.join()
            done.set()

        threading.Thread(target=joiner, daemon=True).start()

        assert done.wait(timeout=10), "throttle deadlocked under concurrency"
        assert not errors

    def test_host_extraction(self):
        assert Throttle.host_of("https://api.example.com/v1/x") == "api.example.com"
