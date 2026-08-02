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

from ..chains import address_key
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
    -- Identity is asserted by ux_tr_identity below rather than by an inline
    -- UNIQUE, because it needs an expression and SQLite allows those only in
    -- an index.
    source        TEXT
);

-- What makes two rows the same transfer.
--
-- `asset` was missing from this key, so two transfers of equal raw amounts of
-- *different* tokens, in one transaction between one pair of addresses,
-- collided -- and INSERT OR IGNORE dropped the second with no error. A DEX
-- routing through two pools, or an airdrop sending equal units of two tokens,
-- produces exactly that shape, and the loss was invisible: the store simply
-- held fewer rows than it was handed.
--
-- COALESCE because SQLite treats NULLs as distinct in a unique index, and a
-- native transfer has no asset -- without it, native transfers would stop
-- deduplicating entirely, which trades a silent loss for a silent duplicate.
--
-- `kind` is here for the same reason `asset` is. A native transfer and an
-- internal one of the same value in the same transaction between the same pair
-- are two different movements -- that is exactly how swap proceeds and
-- withdrawal payouts appear -- and without it one of them was dropped
-- silently. Measured: two rows in, one row out.
--
-- sender and recipient stay bare. They are NULL for mints and burns, where
-- NULL-is-distinct is the behaviour that was already in place; changing it
-- here would quietly start merging rows this store has always kept apart, and
-- that is a separate decision from the one being fixed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tr_identity ON transfers(
    chain, tx_hash, log_index, sender, recipient, amount_raw, COALESCE(asset, ''), kind
);

CREATE INDEX IF NOT EXISTS ix_tr_sender    ON transfers(chain, sender, timestamp);
CREATE INDEX IF NOT EXISTS ix_tr_recipient ON transfers(chain, recipient, timestamp);
CREATE INDEX IF NOT EXISTS ix_tr_block     ON transfers(chain, block);
CREATE INDEX IF NOT EXISTS ix_tr_amount    ON transfers(chain, amount_raw);
CREATE INDEX IF NOT EXISTS ix_tr_asset     ON transfers(chain, asset);

CREATE TABLE IF NOT EXISTS expanded (
    address    TEXT NOT NULL,
    chain      TEXT NOT NULL,
    depth      INTEGER NOT NULL DEFAULT 0,
    at         INTEGER NOT NULL,
    PRIMARY KEY (address, chain)
);
"""

#: The attributions table, held apart from the rest as individual statements.
#:
#: Migration rebuilds this table, and `executescript` cannot be used to do it:
#: Python's sqlite3 issues an implicit COMMIT before running a script, so a
#: migration that used one would be committed halfway through and could not be
#: rolled back. These run through `execute()` inside the migration's
#: transaction instead.
_ATTRIBUTIONS = (
    """
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
        -- Who wrote it down, as against `source`, which is where they got it.
        analyst     TEXT NOT NULL DEFAULT ''
    )
    """,
    # `analyst` belongs in the key for the same reason `asset` and `kind` belong
    # in the transfer key: without it a row is dropped silently. Two analysts
    # reading the same explorer page and both tagging an address produce
    # identical (address, source, label), so INSERT OR IGNORE kept the first and
    # discarded the second -- and the case record then said one person asserted
    # something two people had. In a shared case that is the fact worth keeping.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_attr_identity
        ON attributions(address, source, label, analyst)
    """,
    "CREATE INDEX IF NOT EXISTS ix_attr_address ON attributions(address)",
)


#: Digits used to pad amounts. Total ETH supply is ~1.2e26 wei, so 40 leaves
#: room for any asset that will plausibly exist without wasting index space.
AMOUNT_WIDTH = 40


def _both_spellings(address: str) -> list[str]:
    """The address as written, and lowercased, without duplicates.

    For an unscoped lookup only. Rows written by an EVM chain are lowercase and
    rows written by Solana, Sui or Bitcoin are as given, so a question that
    names no chain has to accept either --- and asking for both is a superset,
    never a false match: two spellings that differ only in case are the same
    address on the one ecosystem where they can both be present.
    """
    text = address.strip()
    return [text] if text == text.lower() else [text, text.lower()]


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
        # Version first, then the attributions table. The order matters on an
        # older store: its `attributions` already exists, so the CREATE below is
        # a no-op and the unique index would be built against a table with no
        # `analyst` column. Migration has to have run by then.
        self._check_schema()
        for statement in _ATTRIBUTIONS:
            self._conn.execute(statement)
        self._conn.commit()

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
        if found == SCHEMA_VERSION:
            return
        if found < SCHEMA_VERSION:
            self.migrate(found)
            return
        # Forward is not migration, it is guessing. A store written by a newer
        # build may hold columns this one drops on the next write.
        raise StoreError(
            f"store at {self.path} uses schema {found}, this build reads "
            f"{SCHEMA_VERSION}. It was written by a newer chainscope; upgrade, "
            f"or rebuild from the cache."
        )

    def migrate(self, from_version: int) -> None:
        """Upgrade an older store in place.

        Only additive steps are automatic. Every migration here adds a column or
        widens a uniqueness key, neither of which can lose a row --- widening a
        unique key can only ever admit more, so it cannot fail against existing
        data. A step that could drop or reinterpret rows does not belong in a
        function that runs without being asked; that is what the rebuild
        guarantee in :mod:`chainscope.store.base` is for.

        Each step runs in one **explicit** transaction. `BEGIN` is issued by
        hand because Python's sqlite3 opens one only for INSERT/UPDATE/DELETE:
        DDL runs in autocommit, so an `ALTER TABLE ... RENAME` commits the
        instant it executes and `rollback()` cannot undo it. Measured --- the
        rename survived a rollback, which would leave a store carrying
        `attributions_v3`, no `attributions`, and a version still reading 3, so
        the next open would retry the rename and fail on the name already
        existing. Unrecoverable without hand-editing the file.

        A half-applied store is indistinguishable from a complete one and would
        be trusted, so this has to actually hold rather than be asserted here.
        """
        version = from_version
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                if version == 3:
                    self._migrate_3_to_4()
                    version = 4
                if version != SCHEMA_VERSION:
                    raise StoreError(
                        f"no automatic migration from schema {from_version} to "
                        f"{SCHEMA_VERSION}. Rebuild from the cache."
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.commit()
            except StoreError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(
                    f"migrating {self.path} from schema {from_version} failed and "
                    f"was rolled back: {exc}"
                ) from exc

    def _migrate_3_to_4(self) -> None:
        """Add `analyst` to attributions and put it in the uniqueness key.

        The key was a table-level `UNIQUE (address, source, label)`, which
        SQLite cannot alter, so the table is rebuilt. Existing rows get an empty
        analyst --- that is the truthful value, since schema 3 recorded no
        authorship and inventing one now would attribute somebody's claims to
        whoever happens to run the upgrade.
        """
        self._conn.execute("ALTER TABLE attributions RENAME TO attributions_v3")
        for statement in _ATTRIBUTIONS:
            self._conn.execute(statement)
        self._conn.execute(
            "INSERT OR IGNORE INTO attributions "
            "(address, chain, label, category, confidence, method, source, "
            " rationale, observed_at, analyst) "
            "SELECT address, chain, label, category, confidence, method, source, "
            "       rationale, observed_at, '' FROM attributions_v3"
        )
        self._conn.execute("DROP TABLE attributions_v3")

    # ---------------------------------------------------------------- writing

    def put_transfers(self, transfers: Iterable[Transfer], *, source: str = "") -> int:
        rows = [
            (
                str(t.chain),
                t.tx.hash,
                t.index,
                # Normalised here rather than trusting `Address.key`. The
                # adapters set it correctly; anything hand-built may not, and a
                # row written with an unnormalised key is a row no query finds
                # again --- write and read have to agree by construction, not by
                # everybody upstream remembering.
                address_key(t.chain, t.sender.raw) if t.sender else None,
                address_key(t.chain, t.recipient.raw) if t.recipient else None,
                _pad(t.amount.raw),
                t.amount.decimals,
                t.amount.symbol,
                address_key(t.chain, t.asset.raw) if t.asset else None,
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
                address_key(a.chain, a.address),
                str(a.chain) if a.chain else None,
                a.label,
                a.category.value,
                int(a.confidence),
                a.method.value,
                a.source,
                a.rationale,
                _ts(a.observed_at),
                a.analyst,
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
                " rationale, observed_at, analyst) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def mark_expanded(self, address: str, chain: ChainId, *, depth: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO expanded VALUES (?,?,?,?)",
                (
                    address_key(chain, address),
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
                (address_key(chain, address), str(chain)),
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
            key = address_key(q.chain, q.address)
            params += [key, key]
        if q.sender:
            clauses.append("sender = ?")
            params.append(address_key(q.chain, q.sender))
        if q.recipient:
            clauses.append("recipient = ?")
            params.append(address_key(q.chain, q.recipient))
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
            (str(chain), address_key(chain, address)),
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

    def attributions(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """Every claim about an address.

        The table is keyed by address alone, because a claim may be
        chain-agnostic --- that is how sanctions lists are published --- so the
        chain cannot be part of the key.

        Given a chain, the lookup is exact for that chain's rules. Without one,
        the question is "anything known about this address", and both the
        as-written and lowercased spellings are tried: the table can hold either
        depending on which chain wrote the row, and an unscoped question should
        not miss a claim because of the ecosystem it came from.
        """
        keys = [address_key(chain, address)] if chain else _both_spellings(address)
        placeholders = ", ".join("?" for _ in keys)
        rows = self._fetch(
            f"SELECT * FROM attributions WHERE address IN ({placeholders}) "
            f"ORDER BY confidence DESC",
            keys,
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
                analyst=r["analyst"],
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
