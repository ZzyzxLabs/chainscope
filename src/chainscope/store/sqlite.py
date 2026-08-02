"""SQLite store.

The default because a default that needs a running server is a default nobody
uses. Recursive CTEs handle the graph sizes an address-scoped investigation
produces; a user who outgrows that swaps the backend without touching an
analyzer.

Amounts are stored as **zero-padded decimal text**, which needs explaining
because it looks like a mistake.

SQLite's INTEGER is 64-bit signed, so it tops out near 9.2e18 --- and 10 ETH is
1e19 wei. Storing raw amounts as INTEGER therefore fails on ordinary values.
REAL would accept them and silently round, which is worse. Text padded to a
fixed width preserves every digit *and* keeps lexicographic ordering identical
to numeric ordering, so `ORDER BY` and `BETWEEN` still work in SQL rather than
having to be re-done in Python over the whole table.

This was found by a smoke test on a 10 ETH transfer. It is exactly the class of
defect the project exists to prevent, in the layer meant to be boring.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..core.models import Address, Transfer, TransferKind, TxRef
from ..core.units import Amount
from .base import SCHEMA_VERSION, EdgeSummary, Query, Store, StoreError, StoreStats

__all__ = ["SqliteStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY,
    chain         TEXT NOT NULL,
    tx_hash       TEXT NOT NULL,
    log_index     INTEGER NOT NULL DEFAULT 0,
    sender        TEXT,
    recipient     TEXT,
    -- Zero-padded decimal text: SQLite INTEGER is 64-bit and overflows at
    -- ~9.2e18, which 10 ETH (1e19 wei) already exceeds. Padding keeps
    -- lexicographic order equal to numeric order so SQL comparison still works.
    amount_raw    TEXT NOT NULL,
    decimals      INTEGER NOT NULL,
    symbol        TEXT NOT NULL DEFAULT '',
    asset         TEXT,
    kind          TEXT NOT NULL,
    block         INTEGER,
    timestamp     INTEGER,
    source        TEXT,
    UNIQUE (chain, tx_hash, log_index, sender, recipient, amount_raw)
);

CREATE INDEX IF NOT EXISTS ix_tr_sender    ON transfers(chain, sender, timestamp);
CREATE INDEX IF NOT EXISTS ix_tr_recipient ON transfers(chain, recipient, timestamp);
CREATE INDEX IF NOT EXISTS ix_tr_block     ON transfers(chain, block);
CREATE INDEX IF NOT EXISTS ix_tr_amount    ON transfers(chain, amount_raw);
CREATE INDEX IF NOT EXISTS ix_tr_asset     ON transfers(chain, asset);

CREATE TABLE IF NOT EXISTS attributions (
    id          INTEGER PRIMARY KEY,
    address     TEXT NOT NULL,
    chain       TEXT,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL,
    confidence  INTEGER NOT NULL,
    method      TEXT NOT NULL,
    source      TEXT NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    observed_at INTEGER,
    UNIQUE (address, source, label)
);
CREATE INDEX IF NOT EXISTS ix_attr_address ON attributions(address);

CREATE TABLE IF NOT EXISTS expanded (
    address    TEXT NOT NULL,
    chain      TEXT NOT NULL,
    depth      INTEGER NOT NULL DEFAULT 0,
    at         INTEGER NOT NULL,
    PRIMARY KEY (address, chain)
);
"""


#: Digits used to pad amounts. Total ETH supply is ~1.2e26 wei, so 40 leaves
#: room for any asset that will plausibly exist without wasting index space.
AMOUNT_WIDTH = 40


def _pad(raw: int) -> str:
    """Fixed-width decimal text whose sort order matches numeric order."""
    if raw < 0:
        raise StoreError(
            "negative transfer amounts are not storable: the padded encoding "
            "that keeps SQL ordering correct assumes non-negative values. "
            "Represent a debit as a transfer in the opposite direction."
        )
    if raw >= 10**AMOUNT_WIDTH:
        raise StoreError(f"amount {raw} exceeds {AMOUNT_WIDTH} digits")
    return str(raw).zfill(AMOUNT_WIDTH)


@dataclass
class _Bucket:
    """Running totals for one address pair. Python ints, so arbitrary precision."""

    sender: str | None
    recipient: str | None
    symbol: str = ""
    decimals: int = 18
    total: int = 0
    count: int = 0
    first: int | None = None
    last: int | None = None

    def add(self, amount: int, timestamp: int | None) -> None:
        self.total += amount
        self.count += 1
        if timestamp is not None:
            self.first = timestamp if self.first is None else min(self.first, timestamp)
            self.last = timestamp if self.last is None else max(self.last, timestamp)


def _ts(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def _dt(value: int | None) -> datetime | None:
    # `is not None`, not truthiness: timestamp 0 is the Unix epoch, a real
    # instant, and 1970 block data exists in test fixtures and some testnets.
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


class SqliteStore(Store):
    """File-backed store. Pass ``:memory:`` for tests."""

    name = "sqlite"

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = Path(path) if path != ":memory:" else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path) if self.path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._check_schema()

    def _check_schema(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self._conn.commit()
            return
        found = int(row["value"])
        if found != SCHEMA_VERSION:
            # Loudly, not optimistically: a partially-understood store gets
            # trusted, and its wrongness is invisible.
            raise StoreError(
                f"store at {self.path} uses schema {found}, this build reads "
                f"{SCHEMA_VERSION}. Migrate, or rebuild from the cache."
            )

    # ---------------------------------------------------------------- writing

    def put_transfers(self, transfers: Iterable[Transfer], *, source: str = "") -> int:
        rows = [
            (
                str(t.chain),
                t.tx.hash,
                t.index,
                t.sender.key if t.sender else None,
                t.recipient.key if t.recipient else None,
                _pad(t.amount.raw),
                t.amount.decimals,
                t.amount.symbol,
                t.asset.key if t.asset else None,
                t.kind.value,
                t.block,
                _ts(t.timestamp),
                source,
            )
            for t in transfers
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO transfers "
                "(chain, tx_hash, log_index, sender, recipient, amount_raw, "
                " decimals, symbol, asset, kind, block, timestamp, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def put_attributions(self, attributions: Iterable[Attribution]) -> int:
        rows = [
            (
                a.address.lower(),
                str(a.chain) if a.chain else None,
                a.label,
                a.category.value,
                int(a.confidence),
                a.method.value,
                a.source,
                a.rationale,
                _ts(a.observed_at),
            )
            for a in attributions
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO attributions "
                "(address, chain, label, category, confidence, method, source, "
                " rationale, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def mark_expanded(self, address: str, chain: ChainId, *, depth: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO expanded VALUES (?,?,?,?)",
                (
                    address.lower(),
                    str(chain),
                    depth,
                    int(datetime.now(timezone.utc).timestamp()),
                ),
            )
            self._conn.commit()

    def is_expanded(self, address: str, chain: ChainId) -> bool:
        return bool(
            self._fetch(
                "SELECT 1 FROM expanded WHERE address = ? AND chain = ?",
                (address.lower(), str(chain)),
            )
        )

    # ---------------------------------------------------------------- access

    def _fetch(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Run a read under the connection lock and materialise the rows.

        Materialising inside the lock, rather than returning a cursor, is the
        point. `transfers()` is a generator, and holding the lock across a yield
        would keep it held for as long as the *caller* takes to process each row
        --- so one slow consumer would serialise every other thread's reads. It
        also means `close()` cannot land mid-iteration.

        Rows are bounded by the query's own LIMIT or by one address's transfer
        count, so materialising is proportionate.
        """
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ---------------------------------------------------------------- reading

    @staticmethod
    def _where(q: Query) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if q.chain:
            clauses.append("chain = ?")
            params.append(str(q.chain))
        if q.address:
            clauses.append("(sender = ? OR recipient = ?)")
            params += [q.address.lower(), q.address.lower()]
        if q.sender:
            clauses.append("sender = ?")
            params.append(q.sender.lower())
        if q.recipient:
            clauses.append("recipient = ?")
            params.append(q.recipient.lower())
        if q.asset:
            clauses.append("asset = ?")
            params.append(q.asset.lower())
        # Bounds are padded identically, so the comparison stays a string
        # comparison that means the right thing.
        if q.min_amount is not None:
            clauses.append("amount_raw >= ?")
            params.append(_pad(q.min_amount))
        if q.max_amount is not None:
            clauses.append("amount_raw <= ?")
            params.append(_pad(q.max_amount))
        if q.after:
            clauses.append("timestamp >= ?")
            params.append(_ts(q.after))
        if q.before:
            clauses.append("timestamp <= ?")
            params.append(_ts(q.before))
        if q.min_block is not None:
            clauses.append("block >= ?")
            params.append(q.min_block)
        if q.max_block is not None:
            clauses.append("block <= ?")
            params.append(q.max_block)
        if q.kinds:
            clauses.append(f"kind IN ({','.join('?' * len(q.kinds))})")
            params += list(q.kinds)
        return (" AND ".join(clauses) or "1=1"), params

    def transfers(self, query: Query) -> Iterator[Transfer]:
        where, params = self._where(query)
        order = {
            "amount": "amount_raw DESC",
            "block": "block ASC",
            "time": "timestamp ASC",
        }.get(query.order, "timestamp ASC")
        for row in self._fetch(
            f"SELECT * FROM transfers WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, query.limit, query.offset],
        ):
            yield self._to_transfer(row)

    def count(self, query: Query) -> int:
        where, params = self._where(query)
        rows = self._fetch(f"SELECT COUNT(*) AS n FROM transfers WHERE {where}", params)
        return int(rows[0]["n"])

    def edges(
        self,
        address: str,
        chain: ChainId,
        *,
        direction: str = "out",
        min_total: int = 0,
    ) -> list[EdgeSummary]:
        """Aggregate flow to or from an address.

        Summed in Python rather than SQL. ``SUM()`` would overflow: SQLite
        integers are 64-bit and a single 10 ETH transfer is already 1e19 wei, so
        even one row can exceed the type before any addition happens. Python
        integers are arbitrary precision, which is the only correct place for
        this arithmetic in a tool whose premise is exactness.

        The row count is bounded by one address's transfers, so pulling them
        into memory is proportionate.
        """
        side, other = ("sender", "recipient") if direction == "out" else ("recipient", "sender")
        rows = self._fetch(
            f"SELECT sender, recipient, asset, amount_raw, timestamp, symbol, decimals "
            f"FROM transfers WHERE chain = ? AND {side} = ?",
            (str(chain), address.lower()),
        )

        buckets: dict[tuple[str | None, str | None], _Bucket] = {}
        for r in rows:
            key = (r[other], r["asset"])
            b = buckets.get(key)
            if b is None:
                b = _Bucket(
                    sender=r["sender"],
                    recipient=r["recipient"],
                    symbol=r["symbol"] or "",
                    decimals=r["decimals"],
                )
                buckets[key] = b
            b.add(int(r["amount_raw"]), r["timestamp"])

        out = [
            EdgeSummary(
                sender=b.sender or "",
                recipient=b.recipient or "",
                asset=asset,
                total_raw=b.total,
                transfer_count=b.count,
                symbol=b.symbol,
                decimals=b.decimals,
                first_seen=_dt(b.first),
                last_seen=_dt(b.last),
            )
            for (_, asset), b in buckets.items()
            if b.total >= min_total
        ]
        out.sort(key=lambda e: -e.total_raw)
        return out

    def attributions(self, address: str) -> list[Attribution]:
        rows = self._fetch(
            "SELECT * FROM attributions WHERE address = ? ORDER BY confidence DESC",
            (address.lower(),),
        )
        return [
            Attribution(
                address=r["address"],
                chain=ChainId.parse(r["chain"]) if r["chain"] else None,
                label=r["label"],
                category=Category(r["category"]),
                confidence=Confidence(r["confidence"]),
                method=Method(r["method"]),
                source=r["source"],
                rationale=r["rationale"],
                observed_at=_dt(r["observed_at"]),
            )
            for r in rows
        ]

    def frontier(self, chain: ChainId) -> list[str]:
        rows = self._fetch(
            "SELECT DISTINCT a FROM ("
            "  SELECT sender AS a FROM transfers WHERE chain = ? AND sender IS NOT NULL"
            "  UNION SELECT recipient FROM transfers WHERE chain = ? AND recipient IS NOT NULL"
            ") WHERE a NOT IN (SELECT address FROM expanded WHERE chain = ?)",
            (str(chain), str(chain), str(chain)),
        )
        return [r["a"] for r in rows]

    def _to_transfer(self, row: sqlite3.Row) -> Transfer:
        chain = ChainId.parse(row["chain"])

        def addr(value: str | None) -> Address | None:
            return Address(chain, value, value) if value else None

        return Transfer(
            chain=chain,
            tx=TxRef(chain, row["tx_hash"]),
            sender=addr(row["sender"]),
            recipient=addr(row["recipient"]),
            amount=Amount(int(row["amount_raw"]), row["decimals"], row["symbol"]),
            kind=TransferKind(row["kind"]),
            timestamp=_dt(row["timestamp"]),
            block=row["block"],
            index=row["log_index"],
            asset=addr(row["asset"]),
        )

    # ---------------------------------------------------------------- lifecycle

    def stats(self) -> StoreStats:
        return StoreStats(
            transfers=int(self._fetch("SELECT COUNT(*) n FROM transfers")[0]["n"]),
            addresses=int(
                self._fetch(
                    "SELECT COUNT(DISTINCT a) n FROM ("
                    "  SELECT sender a FROM transfers UNION SELECT recipient FROM transfers"
                    ") WHERE a IS NOT NULL"
                )[0]["n"]
            ),
            attributions=int(self._fetch("SELECT COUNT(*) n FROM attributions")[0]["n"]),
            chains=[r["chain"] for r in self._fetch("SELECT DISTINCT chain FROM transfers")],
            bytes=self.path.stat().st_size if self.path and self.path.exists() else 0,
        )

    def clear(self) -> None:
        with self._lock:
            for table in ("transfers", "attributions", "expanded"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
