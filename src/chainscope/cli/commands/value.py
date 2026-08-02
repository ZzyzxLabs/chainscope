"""``chainscope value`` --- what it was worth when it moved.

The question 14 of the 55 challenges in the reference set ask in one form or
another, and the one an investigator is asked first by everybody who is not an
investigator: *how much was that?* It is also the question this package had the
machinery for and no way to reach --- :mod:`chainscope.pricing` has had a
minute-resolution rate source with a local cache since early on, and exactly
one caller, buried inside cross-chain matching.

That is §2 of `docs/needs.md` again: a technique nobody can reach does not
exist.

**At the time, not now.** A figure valued at today's rate is a different claim
from one valued when the money moved, and the second is the only one a report
can defend. So every valuation names the rate, the moment it was taken, and
where it came from.

**It never invents a rate, and it says how close the one it used was.** The
rate source answers from a nearby candle when a minute has no trade --- thin
books and maintenance windows leave gaps, and for the search-window sizing it
was built for that is right. For a figure in a report it is only right if the
distance is *stated*, so every valuation prints the gap when there is one, and
anything beyond :data:`MAX_GAP_MINUTES` is refused rather than reported.

This began as a docstring claiming the nearest rate was never used. It was, by
the layer underneath, which stamped it with the minute somebody asked about.
Writing a property down is not the same as having it.

An undated transfer cannot be valued at all, and saying so is the whole of the
correct behaviour there.

**A total is a sum of valuations, not a valuation of a sum.** Ten transfers
across a year, each converted at its own moment, add up to something real; the
same total converted at any single rate does not. The output says which it is,
because the number looks identical either way.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...pricing.base import PriceSource, Quote, RateError
from ...render.base import Renderer

__all__ = ["MAX_GAP_MINUTES", "Valuation", "add_parser", "run", "value_transfers"]

#: How far from the moment asked about a rate may be observed and still used.
#:
#: Fifteen minutes, against the rate source's own default of 120. That default
#: was chosen for cross-chain matching, where a rough rate sizes a search window
#: and being an hour out costs a wider search. Here the number goes in a report,
#: and an hour of a volatile asset is a different figure --- so the bound is
#: tighter, and anything past it is refused with the distance named.
MAX_GAP_MINUTES = 15


class Valuation:
    """One amount, converted at the rate that applied when it moved."""

    __slots__ = ("amount", "at", "quote", "symbol", "value")

    def __init__(self, symbol: str, amount: Decimal, at: datetime, quote: Quote) -> None:
        self.symbol = symbol
        self.amount = amount
        self.at = at
        self.quote = quote
        self.value = quote.convert(amount)


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(
        name, help="what an amount, or an address's flows, were worth when they moved"
    )
    p.add_argument(
        "target",
        nargs="?",
        help="an address whose transfers to value, or a bare amount with --symbol",
    )
    p.add_argument("--symbol", "-s", help="asset symbol for a bare amount, e.g. ETH")
    p.add_argument("--at", help="when, for a bare amount (YYYY-MM-DD or ISO-8601)")
    p.add_argument("--quote", "-q", default="USDT", help="what to value it in")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument(
        "--rates",
        type=Path,
        default=Path(".chainscope/rates.db"),
        help="local rate cache. Populated on first use; reused offline after",
    )
    p.add_argument("--limit", type=int, default=200, help="transfers to value")


def run(args: argparse.Namespace, render: Renderer) -> int:
    if not args.target:
        print(
            "give an address to value, or an amount with --symbol and --at",
            file=sys.stderr,
        )
        return 2

    source = _source(args.rates)
    if args.symbol:
        return _one(args, source)
    return _address(args, source)


def _source(path: Path) -> PriceSource:
    from ...pricing.binance import BinanceKlines

    path.parent.mkdir(parents=True, exist_ok=True)
    return BinanceKlines(path)


def _when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    for shape in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"--at should be YYYY-MM-DD or ISO-8601, got {raw!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _one(args: argparse.Namespace, source: PriceSource) -> int:
    """Value a single amount somebody typed."""
    try:
        amount = Decimal(str(args.target))
    except Exception:
        print(
            f"{args.target!r} is not an amount. Give an address, or a number with --symbol",
            file=sys.stderr,
        )
        return 2
    try:
        at = _when(args.at)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if at is None:
        # Defaulting to now would answer a different question and look identical.
        print(
            "--at is required: an amount has no value without a moment, and "
            "using now\nwould silently answer 'what is it worth today' instead.",
            file=sys.stderr,
        )
        return 2

    try:
        quote = source.rate(args.symbol.upper(), args.quote.upper(), at)
    except RateError as exc:
        print(f"no rate: {exc}", file=sys.stderr)
        return 1
    if quote.gap_minutes > MAX_GAP_MINUTES:
        print(
            f"nearest rate is {quote.gap_minutes} minutes from "
            f"{at:%Y-%m-%d %H:%M}, past the {MAX_GAP_MINUTES}-minute bound.\n"
            f"Refusing rather than reporting it as the rate at that moment.",
            file=sys.stderr,
        )
        return 1

    print(f"{amount} {args.symbol.upper()} = {quote.convert(amount):,.2f} {args.quote.upper()}")
    print(f"  at {at:%Y-%m-%d %H:%M} UTC")
    print(f"  rate {quote}")
    return 0


def value_transfers(
    transfers: list[Any], source: PriceSource, quote_symbol: str
) -> tuple[list[Valuation], list[str]]:
    """Value each transfer at its own timestamp.

    Returns ``(valued, refusals)``. A refusal is a transfer that could not be
    valued and why --- returned rather than dropped, because a total computed
    over the ones that happened to work, presented without the ones that did
    not, is the exact shape of a confidently wrong figure.
    """
    valued: list[Valuation] = []
    refusals: list[str] = []
    for transfer in transfers:
        when = getattr(transfer, "timestamp", None)
        symbol = (getattr(transfer.amount, "symbol", "") or "").upper()
        if when is None:
            # Not valued at "now". A provider omitting a timestamp is not
            # evidence the transfer happened today.
            refusals.append(
                f"{symbol or 'transfer'}: no timestamp, so no moment to value it at"
            )
            continue
        if not symbol:
            refusals.append("a transfer carries no asset symbol; nothing to look up")
            continue
        try:
            rate = source.rate(symbol, quote_symbol, when)
        except RateError as exc:
            refusals.append(f"{symbol} at {when:%Y-%m-%d %H:%M}: {exc}")
            continue
        if rate.gap_minutes > MAX_GAP_MINUTES:
            # Named, with the distance. "No rate" and "a rate from an hour
            # away" are different facts, and only one of them earns silence.
            refusals.append(
                f"{symbol} at {when:%Y-%m-%d %H:%M}: nearest rate is "
                f"{rate.gap_minutes}m away, past the {MAX_GAP_MINUTES}m bound"
            )
            continue
        valued.append(Valuation(symbol, transfer.amount.decimal, when, rate))
    return valued, refusals


def _address(args: argparse.Namespace, source: PriceSource) -> int:
    if not args.store.exists():
        print(f"no store at {args.store}", file=sys.stderr)
        return 2

    from ...store.base import Query
    from ...store.sqlite import SqliteStore

    store = SqliteStore(args.store)
    try:
        transfers = list(store.transfers(Query(address=args.target, limit=args.limit)))
    finally:
        store.close()

    if not transfers:
        print(f"no transfers for {args.target} in {args.store}")
        return 1

    valued, refusals = value_transfers(transfers, source, args.quote.upper())
    quote_symbol = args.quote.upper()

    for item in sorted(valued, key=lambda v: v.at):
        print(
            f"{item.at:%Y-%m-%d %H:%M}  {item.amount:>18,.6f} {item.symbol:<6} "
            f"{item.value:>14,.2f} {quote_symbol}"
        )

    if valued:
        total = sum((v.value for v in valued), Decimal(0))
        print(f"\n{total:,.2f} {quote_symbol} across {len(valued)} transfer(s)")
        # Said every time. The number is identical either way and the claims
        # are not: this is a sum of separate valuations, each at its own
        # moment, not the total converted at one rate.
        print(
            "  Each transfer converted at the rate when it moved, then added. "
            "This is not\n  the same as the total at any single rate, and it is "
            "not what the holdings are\n  worth now."
        )
        spans = [v.at for v in valued]
        print(
            f"  rates from {min(spans):%Y-%m-%d} to {max(spans):%Y-%m-%d}, "
            f"source {valued[0].quote.source}"
        )
        approximate = [v for v in valued if v.quote.gap_minutes]
        if approximate:
            worst = max(v.quote.gap_minutes for v in approximate)
            print(
                f"  {len(approximate)} used a rate from a nearby minute "
                f"(worst {worst}m). Thin books leave gaps; the distance is "
                f"reported rather than smoothed."
            )

    if refusals:
        # Named, and counted against the total. A figure over the transfers
        # that happened to price, with the rest silently absent, is the shape
        # of a confidently wrong answer.
        print(f"\n{len(refusals)} of {len(transfers)} could not be valued:")
        for line in refusals[:10]:
            print(f"  {line}")
        if len(refusals) > 10:
            print(f"  … and {len(refusals) - 10} more")
        print("  The total above excludes them. It is a floor, not a figure.")
    return 0 if valued else 1
