"""Content-addressed response cache with finality-derived expiry.

Two properties drive this design, and they pull in the same direction:

**Correctness.** Chain history below the finality depth is immutable; chain heads
and balances are not. Caching them under one policy is a correctness bug, not
merely a performance one --- a stale head silently answers "what was the latest
block" with yesterday's number, and nothing downstream can tell.

**Reproducibility.** Keys are hashes of the normalised request, so a populated
cache *is* a replayable record of an investigation. Ship the cache and someone
else can rerun your analysis with no API keys and no network. That is the
mechanism behind case bundles, and it is why the store is a plain SQLite file
rather than an in-process dict.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["Cache", "CacheBackend", "CachePolicy", "Volatility", "cache_key"]


class Volatility(str, Enum):
    """How quickly a response stops being true.

    The caller states what *kind* of thing it asked for; the cache decides how
    long that stays valid. Callers should not invent their own TTLs --- that is
    how inconsistent expiry creeps in.
    """

    IMMUTABLE = "immutable"
    """Finalised history: a mined transaction, a block, a receipt. Never expires."""

    SETTLED = "settled"
    """Historical but theoretically reorganisable. Long TTL."""

    SLOW = "slow"
    """Aggregates that drift: address statistics, label snapshots."""

    LIVE = "live"
    """Balances, nonces, mempool. Short TTL."""

    HEAD = "head"
    """The chain tip. Seconds."""

    NEVER = "never"
    """Do not cache at all."""


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """TTL in seconds per volatility class. ``None`` means never expire."""

    immutable: float | None = None
    settled: float | None = 86_400 * 30
    slow: float | None = 3_600
    live: float | None = 60
    head: float | None = 5

    def ttl(self, v: Volatility) -> float | None:
        if v is Volatility.NEVER:
            return 0.0
        return {
            Volatility.IMMUTABLE: self.immutable,
            Volatility.SETTLED: self.settled,
            Volatility.SLOW: self.slow,
            Volatility.LIVE: self.live,
            Volatility.HEAD: self.head,
        }[v]


def cache_key(*parts: Any) -> str:
    """Stable hash of a request.

    ``sort_keys`` matters: two callers building the same query with differently
    ordered kwargs must hit the same entry, or the cache silently halves its
    own hit rate.
    """
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


@runtime_checkable
class CacheBackend(Protocol):
    """What :class:`~chainscope.transport.http.Client` needs from a cache.

    Two methods, because that is genuinely all the transport uses, and a
    narrower surface is what makes the interesting substitutions possible: a
    :class:`~chainscope.transport.cassette.Cassette` replaying committed
    fixtures, a shared Redis instance for a team working one case, a read-only
    view over a bundle someone handed you.

    Expiry is the backend's business, not the caller's. The caller states what
    *kind* of thing it asked for via :class:`Volatility`; a backend is free to
    honour that with a TTL, to ignore it because its contents are a fixed
    recording, or to refuse to serve anything at all.
    """

    def get(self, key: str, volatility: Volatility) -> Any | None: ...

    def put(
        self, key: str, value: Any, volatility: Volatility, *, provider: str | None = None
    ) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    volatility TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    provider   TEXT,
    hits       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_entries_stored ON entries(stored_at);
"""


class Cache:
    """SQLite-backed response cache.

    Thread-safe for the concurrency pattern this project uses (a bounded pool
    of workers fanning out block or address queries). SQLite serialises writes
    itself; the lock here only guards connection setup.
    """

    def __init__(
        self,
        path: Path | str,
        policy: CachePolicy | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.policy = policy or CachePolicy()
        self.enabled = enabled
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- internals

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(self.path, check_same_thread=False)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(_SCHEMA)
                    conn.commit()
                    self._conn = conn
        return self._conn

    # ---------------------------------------------------------------- public

    def get(self, key: str, volatility: Volatility) -> Any | None:
        """Return the cached value, or ``None`` if absent or expired."""
        if not self.enabled:
            return None
        ttl = self.policy.ttl(volatility)
        if ttl == 0.0:
            return None
        row = (
            self._db()
            .execute("SELECT value, stored_at FROM entries WHERE key = ?", (key,))
            .fetchone()
        )
        if row is None:
            return None
        value, stored_at = row
        if ttl is not None and (time.time() - stored_at) > ttl:
            return None
        self._db().execute("UPDATE entries SET hits = hits + 1 WHERE key = ?", (key,))
        self._db().commit()
        return json.loads(value)

    def put(
        self,
        key: str,
        value: Any,
        volatility: Volatility,
        *,
        provider: str | None = None,
    ) -> None:
        if not self.enabled or self.policy.ttl(volatility) == 0.0:
            return
        self._db().execute(
            "INSERT OR REPLACE INTO entries "
            "(key, value, volatility, stored_at, provider, hits) "
            "VALUES (?, ?, ?, ?, ?, COALESCE("
            "  (SELECT hits FROM entries WHERE key = ?), 0))",
            (key, json.dumps(value, default=str), volatility.value, time.time(), provider, key),
        )
        self._db().commit()

    def purge_expired(self) -> int:
        """Drop entries past their TTL. Returns the number removed."""
        now = time.time()
        removed = 0
        for v in Volatility:
            ttl = self.policy.ttl(v)
            if ttl is None or ttl == 0.0:
                continue
            cur = self._db().execute(
                "DELETE FROM entries WHERE volatility = ? AND stored_at < ?",
                (v.value, now - ttl),
            )
            removed += cur.rowcount
        self._db().commit()
        return removed

    def stats(self) -> dict[str, Any]:
        total, hits = (
            self._db()
            .execute("SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM entries")
            .fetchone()
        )
        by_vol = dict(
            self._db()
            .execute("SELECT volatility, COUNT(*) FROM entries GROUP BY volatility")
            .fetchall()
        )
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "entries": total,
            "total_hits": hits,
            "by_volatility": by_vol,
            "bytes": size,
            "path": str(self.path),
        }

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
