"""``chainscope dashboard`` --- what this case contains, and what is unfinished.

The graph command answers "where did the money go". This answers what comes
before and after: how much of the case is actually known, what was never
followed up, and which claims are weak enough that a reviewer will ask.

The assembly here is deliberately plain SQL over the store rather than a pass
through the analytical view. A dashboard that needs a rebuild before it can
render is a dashboard nobody opens mid-investigation, and every figure on it is
a count or an exact sum --- both of which SQLite answers directly, with the sums
done in Python because wei does not fit in a 64-bit integer.
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.attribution import Confidence
from ...render.base import Renderer
from ...render.dashboard import CaseSummary, to_dashboard

__all__ = ["add_parser", "run"]


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="render a case overview from the store")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument("--out", "-o", type=Path, help="write here instead of stdout")
    p.add_argument("--title", default="", help="heading for the page")
    p.add_argument("--top", type=int, default=15, help="how many flows to list")


def run(args: argparse.Namespace, render: Renderer) -> int:
    if not args.store.exists():
        _err(f"no store at {args.store}. Run an analysis first.")
        return 1

    summary = build_summary(args.store, title=args.title, top=args.top)
    page = to_dashboard(summary)

    if args.out:
        out = args.out if args.out.suffix else args.out.with_suffix(".html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        print(
            f"{out}: {summary.transfers:,} transfers across "
            f"{summary.addresses:,} addresses, {summary.coverage:.0%} attributed"
        )
        # Repeated on the terminal because these are the numbers somebody
        # should see before they quote the others.
        if summary.unlabelled:
            print(f"  {summary.unlabelled:,} addresses carry no attribution")
        if summary.low_confidence:
            print(f"  {summary.low_confidence:,} claims at low confidence or below")
    else:
        print(page)
    return 0


def build_summary(store_path: Path, *, title: str = "", top: int = 15) -> CaseSummary:
    """Read a store into a :class:`CaseSummary`.

    Opened read-only. A dashboard is a view, and a view that can write to what
    it is describing is a view that can be blamed for it.
    """
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _summarise(conn, store_path, title=title, top=top)
    finally:
        conn.close()


def _summarise(
    conn: sqlite3.Connection, store_path: Path, *, title: str, top: int
) -> CaseSummary:
    transfers = conn.execute("SELECT COUNT(*) n FROM transfers").fetchone()["n"]
    addresses = conn.execute(
        "SELECT COUNT(DISTINCT a) n FROM ("
        "  SELECT sender AS a FROM transfers WHERE sender IS NOT NULL"
        "  UNION SELECT recipient FROM transfers WHERE recipient IS NOT NULL)"
    ).fetchone()["n"]
    chains = [r["chain"] for r in conn.execute("SELECT DISTINCT chain FROM transfers")]

    attributed: set[str] = set()
    categories: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    attributions = 0
    low_confidence = 0
    try:
        for row in conn.execute(
            "SELECT address, category, source, confidence FROM attributions"
        ):
            attributions += 1
            attributed.add(row["address"].lower())
            categories[row["category"]] += 1
            sources[row["source"]] += 1
            if row["confidence"] <= Confidence.LOW:
                low_confidence += 1
    except sqlite3.Error:
        # An older store may predate the table. Reporting zero claims is honest;
        # failing to render the rest of the case is not.
        pass

    seen = {
        r["a"].lower()
        for r in conn.execute(
            "SELECT sender AS a FROM transfers WHERE sender IS NOT NULL "
            "UNION SELECT recipient FROM transfers WHERE recipient IS NOT NULL"
        )
        if r["a"]
    }
    unlabelled = len(seen - attributed)

    expanded: set[str] = set()
    # An older store may predate the table; a case with nothing marked expanded
    # reports every address as frontier, which overstates the work outstanding
    # rather than understating it. That is the right way round to be wrong.
    with contextlib.suppress(sqlite3.Error):
        expanded = {r["address"].lower() for r in conn.execute("SELECT address FROM expanded")}
    frontier = len(seen - expanded)

    # Summed in Python: SQLite's INTEGER is 64-bit and one 10 ETH transfer is
    # already 1e19 wei, so SUM() overflows before it has added anything.
    # Keyed by (symbol, decimals), not by symbol. The renderer had no decimals
    # to work with and assumed 18, so 1,000 USDC --- six decimals --- appeared
    # on the dashboard as 0.000000. And summing across decimals produces an
    # integer denominated in nothing: 1000 USDC(6) plus 1 USDC(18) is neither
    # 1001 nor anything else. The flows query on the next lines has been reading
    # `decimals` all along; this one did not.
    totals: dict[tuple[str, int | None], list[int]] = {}
    for row in conn.execute("SELECT symbol, decimals, amount_raw FROM transfers"):
        places = row["decimals"]
        asset = (row["symbol"] or "", int(places) if places is not None else None)
        bucket = totals.setdefault(asset, [0, 0])
        bucket[0] += int(row["amount_raw"])
        bucket[1] += 1

    flows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT sender, recipient, symbol, decimals, amount_raw FROM transfers "
        "WHERE sender IS NOT NULL AND recipient IS NOT NULL"
    ):
        key = (row["sender"], row["recipient"], row["symbol"] or "")
        flow = flows.get(key)
        if flow is None:
            flow = {
                "sender": row["sender"],
                "recipient": row["recipient"],
                "symbol": row["symbol"] or "",
                "decimals": row["decimals"],
                "_total": 0,
                "transfers": 0,
            }
            flows[key] = flow
        flow["_total"] += int(row["amount_raw"])
        flow["transfers"] += 1

    # Ranked within an asset, then interleaved. Ranking across assets would put
    # a wei-denominated total above a six-decimal one purely because its raw
    # integer is larger, which says nothing about value.
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for flow in flows.values():
        by_asset.setdefault(str(flow["symbol"]), []).append(flow)
    ranked: list[dict[str, Any]] = []
    for group in by_asset.values():
        group.sort(key=lambda f: -int(f["_total"]))
    while len(ranked) < top and any(by_asset.values()):
        for group in by_asset.values():
            if group and len(ranked) < top:
                ranked.append(group.pop(0))

    top_flows = [
        {
            "sender": f["sender"],
            "recipient": f["recipient"],
            "symbol": f["symbol"],
            "decimals": f["decimals"],
            "total_raw": str(f["_total"]),
            "transfers": f["transfers"],
        }
        for f in ranked
    ]

    span = conn.execute(
        "SELECT MIN(timestamp) lo, MAX(timestamp) hi FROM transfers WHERE timestamp IS NOT NULL"
    ).fetchone()

    return CaseSummary(
        title=title or f"{store_path.stem} — chainscope",
        store_path=str(store_path),
        chains=sorted(chains),
        transfers=transfers,
        addresses=addresses,
        attributions=attributions,
        unlabelled=unlabelled,
        frontier=frontier,
        low_confidence=low_confidence,
        totals_by_asset=sorted(
            (
                (sym, str(total), count, places)
                for (sym, places), (total, count) in totals.items()
            ),
            key=lambda row: -row[2],
        ),
        top_flows=top_flows,
        categories=categories.most_common(),
        sources=sources.most_common(),
        first_seen=_iso(span["lo"]),
        last_seen=_iso(span["hi"]),
    )


def _iso(value: int | None) -> str:
    # `is not None`, not truthiness: timestamp 0 is the Unix epoch, not absence.
    if value is None:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
