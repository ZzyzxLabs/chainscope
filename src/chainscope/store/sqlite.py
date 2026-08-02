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
from collections.abc import Iterable, Iterator
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


def _ts(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def _dt(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None


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
                str(t.chain), t.tx.hash, t.index,
                t.sender.key if t.sender else None,
                t.recipient.key if t.recipient else None,
                _pad(t.amount.raw), t.amount.decimals, t.amount.symbol,
                t.asset.key if t.asset else None,
                t.kind.value, t.block, _ts(t.timestamp), source,
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
                a.address.lower(), str(a.chain) if a.chain else None, a.label,
                a.category.value, int(a.confidence), a.method.value, a.source,
                a.rationale, _ts(a.observed_at),
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
                (address.lower(), str(chain), depth,
                 int(datetime.now(timezone.utc).timestamp())),
            )
            self._conn.commit()

    def is_expanded(self, address: str, chain: ChainId) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM expanded WHERE address = ? AND chain = ?",
                (address.lower(), str(chain)),
            ).fetchone()
            is not None
        )

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
        rows = self._conn.execute(
            f"SELECT * FROM transfers WHERE {where} ORDER BY {order} "
            f"LIMIT ? OFFSET ?",
            [*params, query.limit, query.offset],
        )
        for row in rows:
            yield self._to_transfer(row)

    def count(self, query: Query) -> int:
        where, params = self._where(query)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM transfers WHERE {where}", params
        ).fetchone()
        return int(row["n"])

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
        side, other = (
            ("sender", "recipient") if direction == "out" else ("recipient", "sender")
        )
        rows = self._conn.execute(
            f"SELECT sender, recipient, asset, amount_raw, timestamp "
            f"FROM transfers WHERE chain = ? AND {side} = ?",
            (str(chain), address.lower()),
        ).fetchall()

        buckets: dict[tuple[str | None, str | None], dict[str, object]] = {}
        for r in rows:
            key = (r[other], r["asset"])
            b = buckets.setdefault(
                key,
                {"total": 0, "n": 0, "first": None, "last": None,
                 "sender": r["sender"], "recipient": r["recipient"]},
            )
            b["total"] = int(b["total"]) + int(r["amount_raw"])
            b["n"] = int(b["n"]) + 1
            ts = r["timestamp"]
            if ts is not None:
                b["first"] = ts if b["first"] is None else min(int(b["first"]), ts)
                b["last"] = ts if b["last"] is None else max(int(b["last"]), ts)

        out = [
            EdgeSummary(
                sender=str(b["sender"] or ""),
                recipient=str(b["recipient"] or ""),
                asset=asset,
                total_raw=int(b["total"]),
                transfer_count=int(b["n"]),
                first_seen=_dt(b["first"]),  # type: ignore[arg-type]
                last_seen=_dt(b["last"]),  # type: ignore[arg-type]
            )
            for (_, asset), b in buckets.items()
            if int(b["total"]) >= min_total
        ]
        out.sort(key=lambda e: -e.total_raw)
        return out

    def attributions(self, address: str) -> list[Attribution]:
        rows = self._conn.execute(
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
        rows = self._conn.execute(
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
        c = self._conn
        return StoreStats(
            transfers=int(c.execute("SELECT COUNT(*) n FROM transfers").fetchone()["n"]),
            addresses=int(
                c.execute(
                    "SELECT COUNT(DISTINCT a) n FROM ("
                    "  SELECT sender a FROM transfers UNION SELECT recipient FROM transfers"
                    ") WHERE a IS NOT NULL"
                ).fetchone()["n"]
            ),
            attributions=int(
                c.execute("SELECT COUNT(*) n FROM attributions").fetchone()["n"]
            ),
            chains=[
                r["chain"] for r in c.execute("SELECT DISTINCT chain FROM transfers")
            ],
            bytes=self.path.stat().st_size if self.path and self.path.exists() else 0,
        )

    def clear(self) -> None:
        with self._lock:
            for table in ("transfers", "attributions", "expanded"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
