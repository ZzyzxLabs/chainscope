"""``chainscope sql`` --- query the case with SQL.

The analytics view already existed and only agents could reach it. A person at
a terminal had no way to ask a question the built-in commands do not answer,
which is most questions: "which counterparties received more than 100 ETH in
September", "how many distinct addresses did this contract pay", "what is the
largest single transfer that touched a labelled mixer". Those are the queries
an investigation actually runs, and no fixed set of subcommands covers them.

The view is DuckDB built from the SQLite store, so amounts are exact at 128
bits --- SQLite's INTEGER overflows at ~9.2e18 and ten ether is 1e19 wei, which
means a naive SUM over a case is not merely imprecise, it is wrong by whatever
wrapped. That is the reason this layer exists at all.

**Read-only, and that is enforced twice.** The connection is opened with
external access disabled, which is the real boundary; ``assert_read_only_sql``
sits in front of it so a mistake fails with an explanation rather than a DuckDB
error about a permission nobody knew existed. Chained statements are refused:
only the first would be checked, and DuckDB's execute runs them all.

**Every result says whether it is complete.** A ``LIMIT`` that trims the answer
is reported, because a query returning exactly fifty rows looks identical
whether fifty was the answer or the cap.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from ...render.base import Renderer
from ...store.analytics import AnalyticsError, AnalyticsView, UnsafeQuery

__all__ = ["add_parser", "run"]

#: Shown by ``--schema``. Hand-written rather than reflected because the useful
#: part is *what the columns mean* --- that amounts are raw integers needing
#: decimals applied, that an address is lowercased --- and a reflected schema
#: says none of that.
_SCHEMA = """tables

  transfers    one row per value movement
    chain        TEXT     CAIP-2, e.g. 'eip155:1'
    tx_hash      TEXT     lowercased
    log_index    INTEGER  position within the transaction
    sender       TEXT     lowercased; NULL for a mint
    recipient    TEXT     lowercased; NULL for a burn
    amount_raw   HUGEINT  smallest unit. Divide by 10^decimals for a human
                          figure; do not compare across assets
    decimals     INTEGER
    symbol       TEXT     display only. Two contracts can share one symbol,
                          so group by `asset`, never by `symbol`
    asset        TEXT     contract address; NULL for the chain's native coin
    kind         TEXT     native | token | internal
    block        BIGINT
    timestamp    TIMESTAMP  UTC, NULL where the provider omitted it

  attributions one row per claim about an address
    address      TEXT     lowercased
    chain        TEXT     NULL means the claim applies on every chain
    label        TEXT
    category     TEXT     cex | dex | bridge | mixer | sanctioned | ...
    confidence   INTEGER  0 speculative .. 4 certain
    method       TEXT     list | label | onchain | heuristic | inference | manual
    source       TEXT     never empty --- the type refuses to be built without it
    rationale    TEXT

examples

  -- who received the most, in one asset
  SELECT recipient, SUM(amount_raw)/1e18 AS eth
  FROM transfers WHERE asset IS NULL AND chain = 'eip155:1'
  GROUP BY recipient ORDER BY eth DESC LIMIT 10;

  -- flows into anything labelled a mixer
  SELECT t.sender, a.label, SUM(t.amount_raw)/1e18 AS eth
  FROM transfers t JOIN attributions a ON a.address = t.recipient
  WHERE a.category = 'mixer' GROUP BY 1, 2 ORDER BY eth DESC;

  -- a day's activity for one address
  SELECT date_trunc('hour', timestamp) AS hour, count(*) AS n
  FROM transfers WHERE sender = lower('0x...') GROUP BY 1 ORDER BY 1;
"""


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="run a read-only SQL query over the case")
    p.add_argument("query", nargs="?", help="SQL. Omit with --schema")
    p.add_argument("--schema", action="store_true", help="show tables, columns, and examples")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument(
        "--view",
        type=Path,
        default=Path(".chainscope/analytics.duckdb"),
        help="where the DuckDB view lives. Rebuilt when older than the store",
    )
    p.add_argument("--limit", "-n", type=int, default=100, help="rows to return")
    p.add_argument(
        "--output",
        "-O",
        default="table",
        choices=["table", "json", "csv"],
        help="csv quotes nothing and is for piping; table is for reading",
    )
    p.add_argument("--rebuild", action="store_true", help="rebuild the view before querying")


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _stale(view: Path, store: Path) -> bool:
    """Whether the view predates the store.

    Comparing mtimes rather than trusting the view: querying a stale view
    returns an answer that is wrong in the most expensive way, because it looks
    exactly like a right one and nothing about the result says which store it
    came from.
    """
    if not view.exists():
        return True
    return view.stat().st_mtime < store.stat().st_mtime


def run(args: argparse.Namespace, render: Renderer) -> int:
    if args.schema:
        print(_SCHEMA)
        return 0
    if not args.query:
        _err("give a query, or --schema to see what there is to query")
        return 2
    if not args.store.exists():
        _err(f"no store at {args.store}. Run an analysis or import labels first.")
        return 1

    view = AnalyticsView(args.view)
    try:
        if args.rebuild or _stale(args.view, args.store):
            stats = view.build_from_sqlite(args.store)
            print(
                f"rebuilt view: {stats.transfers:,} transfers, "
                f"{stats.attributions:,} attributions",
                file=sys.stderr,
            )

        try:
            columns = view.columns(args.query)
            rows = view.sql_limited(args.query, limit=args.limit)
        except UnsafeQuery as exc:
            _err(f"refused: {exc}")
            return 2
        except AnalyticsError as exc:
            _err(str(exc))
            return 1

        _emit(columns, rows, args.output)

        if len(rows) >= args.limit:
            # Not a footnote. A query returning exactly `limit` rows looks the
            # same whether that was the answer or the cap, and a total computed
            # from a capped set is a lower bound presented as a number.
            _err(
                f"\n{len(rows)} rows returned, which is the --limit. There are "
                f"probably more, and any aggregate over this set is a lower "
                f"bound. Raise -n or add your own LIMIT."
            )
            return 1
    finally:
        view.close()
    return 0


def _emit(columns: list[str], rows: list[tuple[Any, ...]], output: str) -> None:
    if output == "json":
        print(
            json.dumps([dict(zip(columns, _jsonable(r), strict=False)) for r in rows], indent=2)
        )
        return
    if output == "csv":
        # csv.writer, not join(","). A label containing a comma, a quote, or a
        # newline is ordinary --- "Binance 14, hot" is a plausible nametag ---
        # and joining produces a file whose columns silently shift from that
        # row onward. The command advertises pipeable output; malformed CSV
        # that parses into the wrong columns is worse than none.
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([["" if v is None else str(v) for v in row] for row in rows])
        return

    if not rows:
        print("(no rows)")
        return
    widths = [
        max(len(str(c)), *(len(_cell(r[i])) for r in rows)) for i, c in enumerate(columns)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_cell(v).ljust(w) for v, w in zip(row, widths, strict=True)))


def _cell(value: Any) -> str:
    # NULL rendered as an empty string would be indistinguishable from an empty
    # one, and in this schema NULL means something specific: no asset means the
    # native coin, no chain means every chain.
    return "NULL" if value is None else str(value)


def _jsonable(row: tuple[Any, ...]) -> list[Any]:
    out: list[Any] = []
    for value in row:
        # Integers here are HUGEINT and routinely exceed 2^53. A JSON consumer
        # reading them as floats loses wei, so they leave as strings.
        out.append(str(value) if isinstance(value, int) and abs(value) > 2**53 else value)
    return out
