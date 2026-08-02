"""The analytical view: aggregates, dashboards, and an SQL surface.

A second engine over the same facts, because the two access patterns this tool
has pull in opposite directions and one index cannot serve both.

Traversal asks *"what did this one address do"*, constantly --- every graph
walk, every expansion, every hop. That is a point lookup, and a B-tree answers
it in well under a millisecond while a columnar scan takes ten times longer.
Analysis asks *"sum every USDC transfer above ten thousand in August"*, and
there the ordering reverses by two orders of magnitude. Measured on two million
transfers:

===========================  ==========  ==========
query                            SQLite      DuckDB
===========================  ==========  ==========
``edges(address)``              0.7 ms      6.3 ms
filtered scan                   669 ms      2.7 ms
exact ``SUM`` over an asset     277 ms      5.4 ms
on disk                         769 MB      191 MB
===========================  ==========  ==========

So SQLite keeps the write path and the traversal, and this module is a *derived*
view: rebuildable, disposable, never a source of truth. That is the same rule
the store itself follows (ARCHITECTURE §4.8) applied one level further out. If
this file is deleted, nothing is lost but the time to rebuild it.

**Why the amounts change type here.** The store keeps ``amount_raw`` as
zero-padded text because SQLite's ``INTEGER`` is 64-bit and wei is not: the
exact ETH total across a two-million-transfer sample came to 3.0e27, which is
325 million times SQLite's ceiling. Summing therefore happens in Python, one
row at a time. DuckDB's ``HUGEINT`` is 128 bits, so the same sum runs in the
engine, exactly, fifty times faster. Both were checked to the digit --- exact
arithmetic is not what this trades away.

**On running arbitrary SQL.** :meth:`AnalyticsView.sql` is meant to be reachable
from a CLI, a notebook, and eventually an agent. DuckDB can read and write local
files and load extensions, so a connection handed to any of those without
restriction is a file-read primitive rather than a query interface. The
connection is opened with external access disabled and statements are checked
before execution; see :func:`assert_read_only_sql`.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.chainid import ChainId
from .base import StoreError

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "AnalyticsError",
    "AnalyticsView",
    "BuildStats",
    "Flow",
    "UnsafeQuery",
    "assert_read_only_sql",
]


class AnalyticsError(StoreError):
    """The analytical view could not be built or queried."""


class UnsafeQuery(AnalyticsError):
    """A statement was rejected before it ran.

    Separate from a syntax error because the caller needs to tell "you typed
    this wrong" apart from "you are not allowed to do this here".
    """


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AnalyticsError(
            "the analytical view needs duckdb: pip install 'chainscope[analytics]'\n"
            "The traversal store works without it; this is the layer that adds "
            "aggregates, dashboards, and SQL."
        ) from exc
    return duckdb


# --------------------------------------------------------------------- safety

#: Statements that read. Everything else is refused, including DDL --- a view
#: that can be reshaped by a query is no longer reproducible from the store.
_ALLOWED_LEADS = ("select", "with", "explain", "describe", "summarize", "show", "pragma")

#: Constructs that reach outside the database. DuckDB's file functions are the
#: reason an unrestricted connection is a filesystem primitive: `read_csv` and
#: friends take a path, and `COPY ... TO` writes one.
_FORBIDDEN = (
    "attach",
    "copy",
    "install",
    "load",
    "export",
    "import",
    "read_csv",
    "read_parquet",
    "read_json",
    "read_text",
    "read_blob",
    "glob",
    "delete",
    "insert",
    "update",
    "drop",
    "create",
    "alter",
    "truncate",
    "call",
    "set ",
    "reset",
)

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:[^']|'')*'")


def assert_read_only_sql(query: str) -> None:
    """Reject anything that writes, reaches outside the database, or chains.

    Comments and string literals are stripped before matching, so a forbidden
    word inside a label --- searching for the string ``'drop'`` is a perfectly
    ordinary thing to do --- does not trip the check, and a forbidden statement
    hidden behind a comment does not slip past it.

    This is a guard rail, not a security boundary. It sits in front of a
    connection already opened with external access disabled; that setting is the
    real control, and this exists so a mistake fails with an explanation rather
    than a DuckDB error about a permission the caller never knew existed.
    """
    stripped = _STRING.sub("''", _COMMENT.sub(" ", query)).strip().lower()
    if not stripped:
        raise UnsafeQuery("empty query")

    # Multiple statements: only the first would be checked otherwise.
    if ";" in stripped.rstrip().rstrip(";"):
        raise UnsafeQuery(
            "one statement at a time. Chained statements are refused because "
            "only the first would be checked."
        )

    if not stripped.startswith(_ALLOWED_LEADS):
        raise UnsafeQuery(
            f"only read statements are allowed here ({', '.join(_ALLOWED_LEADS)}). "
            f"The view is derived from the store --- change the store and rebuild, "
            f"rather than editing the view, or the two stop agreeing."
        )

    for word in _FORBIDDEN:
        if re.search(rf"\b{re.escape(word.strip())}\b", stripped):
            raise UnsafeQuery(
                f"{word.strip()!r} is not available here. The analytical view "
                f"reads the store and nothing else --- notably not the filesystem."
            )


# --------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class Flow:
    """Aggregate movement between two addresses in one asset."""

    sender: str
    recipient: str
    asset: str | None
    symbol: str
    total_raw: int
    transfer_count: int
    first_seen: int | None = None
    last_seen: int | None = None

    @property
    def decimal(self) -> str:
        """Human-scale rendering. Kept separate from :attr:`total_raw`, which
        stays an exact integer for every purpose that matters."""
        return str(self.total_raw)


@dataclass
class BuildStats:
    transfers: int = 0
    attributions: int = 0
    seconds: float = 0.0
    bytes: int = 0
    source: str = ""


# --------------------------------------------------------------------- view


_VIEW_SCHEMA = """
CREATE TABLE transfers (
    chain      VARCHAR NOT NULL,
    tx_hash    VARCHAR NOT NULL,
    log_index  INTEGER NOT NULL DEFAULT 0,
    sender     VARCHAR,
    recipient  VARCHAR,
    amount_raw HUGEINT NOT NULL,   -- 128-bit: wei fits, sums stay exact
    decimals   INTEGER NOT NULL,
    symbol     VARCHAR NOT NULL DEFAULT '',
    asset      VARCHAR,
    kind       VARCHAR NOT NULL,
    block      BIGINT,
    timestamp  BIGINT,
    source     VARCHAR
);

CREATE TABLE attributions (
    address     VARCHAR NOT NULL,
    chain       VARCHAR,
    label       VARCHAR NOT NULL,
    category    VARCHAR NOT NULL,
    confidence  INTEGER NOT NULL,
    method      VARCHAR NOT NULL,
    source      VARCHAR NOT NULL,
    rationale   VARCHAR NOT NULL DEFAULT '',
    observed_at BIGINT
);
"""


@dataclass
class AnalyticsView:
    """A DuckDB view derived from a :class:`~chainscope.store.base.Store`.

    Open it, build it from a store, query it, throw it away. Nothing here is
    authoritative; everything is reconstructible.
    """

    path: Path | str = ":memory:"
    _conn: Any = field(default=None, repr=False)
    _built_from: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.path != ":memory:":
            self.path = Path(self.path)

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> Any:
        if self._conn is None:
            duckdb = _duckdb()
            target = str(self.path)
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            # `enable_external_access` is the real boundary. With it on, any
            # query is a file read: DuckDB's table functions take paths, and
            # COPY writes them. assert_read_only_sql is the friendly message in
            # front of this, not a substitute for it.
            self._conn = duckdb.connect(
                target,
                config={
                    "enable_external_access": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                },
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> AnalyticsView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- building

    def build_from_sqlite(self, store_path: Path | str, *, batch: int = 50_000) -> BuildStats:
        """Populate from a SQLite store file.

        Reads through the SQLite driver rather than DuckDB's ``sqlite`` scanner
        on purpose. The scanner would be less code, but it pushes no index down
        and cannot cast the padded text amounts without a full scan anyway ---
        measured at ~95 ms per query against 2.7 ms once the rows are actually
        in DuckDB. Since this is a build step, paying once is the right trade.

        External access is disabled on the connection, so the scanner is not
        available here in any case.
        """
        source = Path(store_path)
        if not source.is_file():
            raise AnalyticsError(f"no store at {source}")

        started = time.perf_counter()
        conn = self.connect()
        conn.execute("DROP TABLE IF EXISTS transfers")
        conn.execute("DROP TABLE IF EXISTS attributions")
        conn.execute(_VIEW_SCHEMA)

        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            transfers = self._copy_transfers(conn, src, batch)
            attributions = self._copy_attributions(conn, src, batch)
        finally:
            src.close()

        self._index()
        self._built_from = str(source)

        size = 0
        if self.path != ":memory:":
            p = Path(self.path)
            size = p.stat().st_size if p.is_file() else 0

        return BuildStats(
            transfers=transfers,
            attributions=attributions,
            seconds=time.perf_counter() - started,
            bytes=size,
            source=str(source),
        )

    def _copy_transfers(self, conn: Any, src: Any, batch: int) -> int:
        cursor = src.execute(
            "SELECT chain, tx_hash, log_index, sender, recipient, amount_raw, "
            "decimals, symbol, asset, kind, block, timestamp, source FROM transfers"
        )
        total = 0
        while True:
            rows = cursor.fetchmany(batch)
            if not rows:
                break
            # int() on the padded text is the one place the representations
            # meet. Doing it here rather than in SQL keeps the exactness
            # guarantee in Python's arbitrary-precision integers all the way to
            # DuckDB's HUGEINT, with no float or 64-bit step in between.
            converted = [
                (
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    int(r[5]),
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                    r[10],
                    r[11],
                    r[12],
                )
                for r in rows
            ]
            conn.executemany(
                "INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", converted
            )
            total += len(converted)
        return total

    def _copy_attributions(self, conn: Any, src: Any, batch: int) -> int:
        try:
            cursor = src.execute(
                "SELECT address, chain, label, category, confidence, method, "
                "source, rationale, observed_at FROM attributions"
            )
        except sqlite3.OperationalError as exc:
            # Only the case this was ever meant to tolerate: a store written
            # before the table existed. Catching everything turned a locked
            # database, a corrupt file, or an incompatible schema into a
            # successful build reporting zero labels --- a view that silently
            # lost every attribution in the case, and looked fine doing it.
            if "no such table" not in str(exc).lower():
                raise AnalyticsError(f"could not read attributions: {exc}") from exc
            return 0
        total = 0
        while True:
            rows = cursor.fetchmany(batch)
            if not rows:
                break
            conn.executemany("INSERT INTO attributions VALUES (?,?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
        return total

    def _index(self) -> None:
        conn = self.connect()
        for stmt in (
            "CREATE INDEX ix_an_sender ON transfers(sender)",
            "CREATE INDEX ix_an_recipient ON transfers(recipient)",
            "CREATE INDEX ix_an_attr ON attributions(address)",
        ):
            conn.execute(stmt)

    # ---------------------------------------------------------------- querying

    def sql(self, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a read-only statement. See :func:`assert_read_only_sql`."""
        assert_read_only_sql(query)
        try:
            rows: list[tuple[Any, ...]] = self.connect().execute(query, list(params)).fetchall()
            return rows
        except Exception as exc:
            raise AnalyticsError(f"query failed: {exc}") from exc

    def sql_limited(
        self, query: str, limit: int, params: Sequence[Any] = ()
    ) -> list[tuple[Any, ...]]:
        """Run a read-only statement, fetching at most ``limit`` rows.

        ``sql`` materialises the whole result. That is fine for a human at a
        prompt and wrong for anything reachable by an agent, where a query over
        a large store can exhaust the process before the caller ever gets to
        apply its own cap.
        """
        assert_read_only_sql(query)
        try:
            cursor = self.connect().execute(query, list(params))
            rows: list[tuple[Any, ...]] = cursor.fetchmany(max(1, limit))
            return rows
        except Exception as exc:
            raise AnalyticsError(f"query failed: {exc}") from exc

    def columns(self, query: str, params: Sequence[Any] = ()) -> list[str]:
        """Column names for a query, for rendering a table without guessing."""
        assert_read_only_sql(query)
        try:
            cur = self.connect().execute(query, list(params))
        except Exception as exc:
            raise AnalyticsError(f"query failed: {exc}") from exc
        return [d[0] for d in (cur.description or [])]

    # ------------------------------------------------------------ aggregations

    def flows(
        self,
        address: str,
        *,
        chain: ChainId | None = None,
        direction: str = "out",
        limit: int = 100,
        min_total: int = 0,
    ) -> list[Flow]:
        """Aggregate movement to or from an address, largest first.

        The unit a flow diagram draws. Rolling transfers up here rather than in
        the caller means one counterparty with four hundred transfers is one
        row, which is the difference between a graph that renders and one that
        does not.
        """
        if direction not in ("out", "in"):
            raise AnalyticsError(f"direction must be 'out' or 'in', not {direction!r}")

        subject, other = (
            ("sender", "recipient") if direction == "out" else ("recipient", "sender")
        )
        where = [f"{subject} = ?"]
        params: list[Any] = [address]
        if chain is not None:
            where.append("chain = ?")
            params.append(str(chain))

        # Chain and symbol both belong in the grouping. Without chain, native
        # transfers -- which all carry asset IS NULL -- summed ETH and BNB
        # between one pair of addresses into a figure denominated in nothing,
        # and any_value(symbol) then picked one of the two arbitrarily. Symbol
        # is there because one contract address can be reused across chains.
        rows = (
            self.connect()
            .execute(
                f"""
            SELECT {subject}, {other}, asset, symbol, chain, any_value(decimals),
                   SUM(amount_raw), COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM transfers
            WHERE {" AND ".join(where)}
            GROUP BY {subject}, {other}, asset, symbol, chain
            HAVING SUM(amount_raw) >= ?
            ORDER BY SUM(amount_raw) DESC
            LIMIT ?
            """,
                [*params, min_total, limit],
            )
            .fetchall()
        )

        return [
            Flow(
                sender=r[0] if direction == "out" else r[1],
                recipient=r[1] if direction == "out" else r[0],
                asset=r[2],
                symbol=r[3] or "",
                total_raw=int(r[6]),
                transfer_count=r[7],
                first_seen=r[8],
                last_seen=r[9],
            )
            for r in rows
        ]

    def totals_by_asset(
        self, address: str, *, direction: str = "out"
    ) -> list[tuple[str, int, int]]:
        """Exact per-asset totals. The number a report quotes."""
        if direction not in ("out", "in"):
            raise AnalyticsError(f"direction must be 'out' or 'in', not {direction!r}")
        subject = "sender" if direction == "out" else "recipient"
        rows = (
            self.connect()
            .execute(
                # Grouped by asset *identity*, not by the symbol shown to a human.
                # A fake token borrowing a real name is a standard trick, and
                # grouping on display text sums it into the real one's total.
                f"""SELECT COALESCE(symbol, ''), SUM(amount_raw), COUNT(*)
                FROM transfers WHERE {subject} = ?
                GROUP BY chain, asset, symbol ORDER BY 2 DESC""",
                [address],
            )
            .fetchall()
        )
        return [(r[0], int(r[1]), r[2]) for r in rows]

    def counterparties(self, address: str, *, limit: int = 50) -> list[tuple[str, int, int]]:
        """Everyone this address touched, either direction, by transfer count."""
        rows = (
            self.connect()
            .execute(
                """
            SELECT other, COUNT(*) n, SUM(amount_raw) FROM (
                SELECT recipient AS other, amount_raw FROM transfers WHERE sender = ?
                UNION ALL
                SELECT sender AS other, amount_raw FROM transfers WHERE recipient = ?
            ) WHERE other IS NOT NULL
            GROUP BY other ORDER BY n DESC LIMIT ?
            """,
                [address, address, limit],
            )
            .fetchall()
        )
        return [(r[0], r[1], int(r[2])) for r in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.connect()
        transfers = conn.execute("SELECT COUNT(*) FROM transfers").fetchone()
        addresses = conn.execute(
            "SELECT COUNT(DISTINCT a) FROM ("
            "  SELECT sender AS a FROM transfers UNION SELECT recipient FROM transfers)"
        ).fetchone()
        chains = conn.execute("SELECT DISTINCT chain FROM transfers").fetchall()
        return {
            "transfers": transfers[0] if transfers else 0,
            "addresses": addresses[0] if addresses else 0,
            "chains": sorted(c[0] for c in chains),
            "built_from": self._built_from,
        }

    def __repr__(self) -> str:
        return f"<AnalyticsView {self.path} from={self._built_from or 'unbuilt'}>"


@contextmanager
def analytical_view(
    store_path: Path | str, view_path: Path | str = ":memory:"
) -> Iterator[AnalyticsView]:
    """Build a view from a store and dispose of it afterwards."""
    view = AnalyticsView(view_path)
    try:
        view.build_from_sqlite(store_path)
        yield view
    finally:
        view.close()
