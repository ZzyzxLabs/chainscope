"""The cache must not answer 'what is the latest block' with yesterday's number."""

import time

import pytest

from chainscope.transport.cache import Cache, CachePolicy, Volatility, cache_key


@pytest.fixture
def cache(tmp_path):
    with Cache(tmp_path / "test.sqlite") as c:
        yield c


class TestKeys:
    def test_key_is_stable_across_kwarg_order(self):
        # Two callers building the same query differently must share an entry,
        # or the cache quietly halves its own hit rate.
        a = cache_key("tx", {"hash": "0xabc", "chain": "eip155:1"})
        b = cache_key("tx", {"chain": "eip155:1", "hash": "0xabc"})
        assert a == b

    def test_different_queries_differ(self):
        assert cache_key("tx", "0xabc") != cache_key("tx", "0xabd")

    def test_key_is_hex_digest(self):
        k = cache_key("x")
        assert len(k) == 64 and int(k, 16) >= 0


class TestRoundTrip:
    def test_put_then_get(self, cache):
        cache.put("k", {"block": 21_000_000}, Volatility.IMMUTABLE)
        assert cache.get("k", Volatility.IMMUTABLE) == {"block": 21_000_000}

    def test_missing_key_is_none(self, cache):
        assert cache.get("nope", Volatility.IMMUTABLE) is None

    def test_survives_reopen(self, tmp_path):
        path = tmp_path / "persist.sqlite"
        with Cache(path) as c:
            c.put("k", [1, 2, 3], Volatility.IMMUTABLE)
        with Cache(path) as c:
            assert c.get("k", Volatility.IMMUTABLE) == [1, 2, 3]

    def test_overwrite_preserves_hit_count(self, cache):
        cache.put("k", 1, Volatility.SLOW)
        cache.get("k", Volatility.SLOW)
        cache.put("k", 2, Volatility.SLOW)
        assert cache.stats()["total_hits"] == 1


class TestExpiry:
    def test_immutable_never_expires(self, cache):
        cache.put("k", "v", Volatility.IMMUTABLE)
        # Backdate a decade; finalised history is still finalised.
        cache._db().execute(
            "UPDATE entries SET stored_at = ?", (time.time() - 86_400 * 3650,)
        )
        assert cache.get("k", Volatility.IMMUTABLE) == "v"

    def test_head_expires_quickly(self, tmp_path):
        c = Cache(tmp_path / "c.sqlite", CachePolicy(head=1))
        c.put("tip", 21_000_000, Volatility.HEAD)
        assert c.get("tip", Volatility.HEAD) == 21_000_000
        c._db().execute("UPDATE entries SET stored_at = ?", (time.time() - 2,))
        assert c.get("tip", Volatility.HEAD) is None

    def test_never_is_not_stored(self, cache):
        cache.put("k", "v", Volatility.NEVER)
        assert cache.get("k", Volatility.NEVER) is None
        assert cache.stats()["entries"] == 0

    def test_ttl_ladder_is_monotonic(self):
        """Volatility classes must be ordered, or the taxonomy is meaningless."""
        p = CachePolicy()
        assert p.ttl(Volatility.IMMUTABLE) is None       # unbounded
        assert p.ttl(Volatility.SETTLED) > p.ttl(Volatility.SLOW)
        assert p.ttl(Volatility.SLOW) > p.ttl(Volatility.LIVE)
        assert p.ttl(Volatility.LIVE) > p.ttl(Volatility.HEAD)

    def test_purge_removes_only_expired(self, tmp_path):
        c = Cache(tmp_path / "c.sqlite", CachePolicy(head=1, immutable=None))
        c.put("old_head", 1, Volatility.HEAD)
        c.put("history", 2, Volatility.IMMUTABLE)
        c._db().execute(
            "UPDATE entries SET stored_at = ? WHERE key = 'old_head'",
            (time.time() - 100,),
        )
        assert c.purge_expired() == 1
        assert c.get("history", Volatility.IMMUTABLE) == 2


class TestDisabled:
    def test_disabled_cache_stores_nothing(self, tmp_path):
        c = Cache(tmp_path / "c.sqlite", enabled=False)
        c.put("k", "v", Volatility.IMMUTABLE)
        assert c.get("k", Volatility.IMMUTABLE) is None


class TestStats:
    def test_reports_counts_and_hits(self, cache):
        cache.put("a", 1, Volatility.IMMUTABLE)
        cache.put("b", 2, Volatility.LIVE)
        cache.get("a", Volatility.IMMUTABLE)
        cache.get("a", Volatility.IMMUTABLE)
        s = cache.stats()
        assert s["entries"] == 2
        assert s["total_hits"] == 2
        assert s["by_volatility"] == {"immutable": 1, "live": 1}
