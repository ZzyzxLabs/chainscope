"""Minute-resolution historical rates from Binance klines.

Chosen because it is free, needs no key, and goes back years at one-minute
resolution --- which matters when the search window for a cross-chain match is
measured in minutes.

Rates land in a local SQLite store. Prefetching a case's date range before
starting means the analysis runs offline afterwards, which is both faster and
the difference between an investigation you can replay and one you cannot.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .base import PriceSource, Quote, RateError

__all__ = ["BinanceKlines"]

_API = "https://api.binance.com/api/v3/klines"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    minute INTEGER NOT NULL,
    close  TEXT NOT NULL,
    PRIMARY KEY (symbol, minute)
);
"""


class BinanceKlines(PriceSource):
    """One-minute closes, cached locally."""

    name = "binance-klines"

    def __init__(
        self,
        path: Path | str,
        *,
        client: Any = None,
        max_gap_minutes: int = 120,
    ) -> None:
        self.path = Path(path)
        self.client = client
        self.max_gap_minutes = max_gap_minutes
        """How far to look either side when a minute has no candle.

        Thin books and maintenance windows leave gaps. Two hours is generous
        for a liquid pair and still far tighter than the daily granularity most
        free APIs offer."""

        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def is_offline(self) -> bool:
        """True once a cache exists --- prefetched cases need no network."""
        return self.path.exists()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(self.path, check_same_thread=False)
                    conn.executescript(_SCHEMA)
                    conn.commit()
                    self._conn = conn
        return self._conn

    # ---------------------------------------------------------------- storage

    def _stored(self, symbol: str, minute: int) -> Decimal | None:
        row = (
            self._db()
            .execute(
                "SELECT close FROM klines WHERE symbol = ? AND minute = ?",
                (symbol, minute),
            )
            .fetchone()
        )
        if row:
            return Decimal(row[0])
        row = (
            self._db()
            .execute(
                "SELECT close, ABS(minute - ?) AS d FROM klines "
                "WHERE symbol = ? AND minute BETWEEN ? AND ? ORDER BY d LIMIT 1",
                (minute, symbol, minute - self.max_gap_minutes, minute + self.max_gap_minutes),
            )
            .fetchone()
        )
        return Decimal(row[0]) if row else None

    def _store(self, symbol: str, rows: list[list[Any]]) -> int:
        self._db().executemany(
            "INSERT OR IGNORE INTO klines VALUES (?, ?, ?)",
            [(symbol, int(r[0]) // 60_000, str(r[4])) for r in rows],
        )
        self._db().commit()
        return len(rows)

    # ---------------------------------------------------------------- network

    def _fetch(self, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
        if self.client is None:
            raise RateError(
                f"no cached rate for {symbol} and no HTTP client configured. "
                f"Prefetch the case's date range first."
            )
        data = self.client.get(
            _API,
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            provider=self.name,
        )
        return data if isinstance(data, list) else []

    def prefetch(self, symbol: str, start: datetime, end: datetime) -> int:
        """Populate the cache for a range. Run this before an investigation."""
        symbol = symbol.upper()
        cursor = int(start.timestamp() * 1000)
        stop = int(end.timestamp() * 1000)
        total = 0
        while cursor < stop:
            rows = self._fetch(symbol, cursor, min(cursor + 1000 * 60_000, stop))
            if not rows:
                break
            total += self._store(symbol, rows)
            cursor = int(rows[-1][0]) + 60_000
            time.sleep(0.1)
        return total

    # ---------------------------------------------------------------- queries

    def _pair(self, symbol: str, at: datetime) -> Decimal | None:
        minute = int(at.timestamp()) // 60
        if (hit := self._stored(symbol, minute)) is not None:
            return hit
        if self.client is None:
            return None
        rows = self._fetch(symbol, minute * 60_000, (minute + 1) * 60_000)
        if not rows:
            return None
        self._store(symbol, rows)
        return Decimal(str(rows[0][4]))

    def rate(self, base: str, quote: str, at: datetime) -> Quote:
        base, quote = base.upper(), quote.upper()
        if at.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if base == quote:
            return Quote(base, quote, Decimal(1), at, self.name, "identity")

        if (direct := self._pair(f"{base}{quote}", at)) is not None:
            return Quote(base, quote, direct, at, self.name, "direct")

        if (inverse := self._pair(f"{quote}{base}", at)) and inverse != 0:
            return Quote(base, quote, Decimal(1) / inverse, at, self.name, "inverted")

        # Triangulate. Carries two spreads instead of one, which the caller
        # needs to know when sizing its tolerance -- hence recording it.
        b = self._pair(f"{base}USDT", at)
        q = self._pair(f"{quote}USDT", at)
        if b and q and q != 0:
            return Quote(base, quote, b / q, at, self.name, "via USDT")

        raise RateError(
            f"no rate for {base}/{quote} at {at.isoformat()}. Tried {base}{quote}, "
            f"{quote}{base}, and triangulation through USDT."
        )

    def coverage(self) -> list[tuple[str, int, datetime, datetime]]:
        rows = (
            self._db()
            .execute(
                "SELECT symbol, COUNT(*), MIN(minute), MAX(minute) "
                "FROM klines GROUP BY symbol ORDER BY symbol"
            )
            .fetchall()
        )
        return [
            (
                s,
                n,
                datetime.fromtimestamp(lo * 60, tz=timezone.utc),
                datetime.fromtimestamp(hi * 60, tz=timezone.utc),
            )
            for s, n, lo, hi in rows
        ]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
