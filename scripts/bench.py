#!/usr/bin/env python3
"""Reproduce the numbers in docs/why-python.md.

Written so the language decision can be re-checked rather than believed. If any
of these move by an order of magnitude, `docs/why-python.md` is out of date and
its conclusion may be too.

Usage:  .venv/bin/python scripts/bench.py [--rows N]
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pathlib
import pstats
import random
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chainscope.analysis.poisoning import find_lookalikes
from chainscope.analysis.route import find_routes
from chainscope.chains import address_key
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.base import Query
from chainscope.store.sqlite import SqliteStore

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _transfers(count: int, addresses: int = 4000) -> list[Transfer]:
    random.seed(0)
    raw = ["0x" + f"{i:040x}" for i in range(addresses)]
    cached = {a: Address(chain=ETHEREUM, raw=a, key=address_key(ETHEREUM, a)) for a in raw}
    return [
        Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, "0x" + f"{i:064x}"),
            index=0,
            sender=cached[random.choice(raw)],
            recipient=cached[random.choice(raw)],
            kind=TransferKind.TOKEN,
            asset=cached[raw[0]],
            amount=Amount(raw=random.randrange(10**18), decimals=18, symbol="USDC"),
            timestamp=T0 + timedelta(seconds=i),
        )
        for i in range(count)
    ]


def store_paths(count: int) -> None:
    rows = _transfers(count)
    path = pathlib.Path(tempfile.mkdtemp()) / "bench.db"
    store = SqliteStore(path)

    start = time.perf_counter()
    store.put_transfers(rows)
    write = time.perf_counter() - start

    start = time.perf_counter()
    read_rows = list(store.transfers(Query(limit=count)))
    read = time.perf_counter() - start
    store.close()

    print(f"  store write   {write:6.2f}s   {count / write:>10,.0f} rows/s")
    print(f"  store read    {read:6.2f}s   {len(read_rows) / read:>10,.0f} rows/s")


def where_the_time_goes(count: int = 60_000) -> None:
    """The share of the write path that is already inside SQLite's C code."""
    rows = _transfers(count)
    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "p.db")
    profiler = cProfile.Profile()
    profiler.enable()
    store.put_transfers(rows)
    profiler.disable()
    store.close()

    buffer = io.StringIO()
    pstats.Stats(profiler, stream=buffer).sort_stats("tottime").print_stats(6)
    print("  write path, by self-time:")
    for line in buffer.getvalue().splitlines()[5:12]:
        if line.strip():
            print("   ", line[:110])


def graph_paths() -> None:
    """The analyzers where a compiled language would help most."""
    random.seed(0)
    addresses = ["0x" + f"{i:040x}" for i in range(5000)]
    for count in (50_000, 200_000):
        rows = [
            SimpleNamespace(
                chain=None,
                sender=SimpleNamespace(key=random.choice(addresses)),
                recipient=SimpleNamespace(key=random.choice(addresses)),
                timestamp=T0 + timedelta(seconds=i),
                asset=None,
                amount=SimpleNamespace(raw=random.randrange(10**18), symbol="ETH"),
                tx=SimpleNamespace(hash=f"0x{i:064x}"),
            )
            for i in range(count)
        ]
        start = time.perf_counter()
        find_routes(rows, addresses[0], addresses[1], max_hops=4)
        route = time.perf_counter() - start
        start = time.perf_counter()
        find_lookalikes(rows, addresses[0])
        poison = time.perf_counter() - start
        print(f"  {count:>7,} transfers   route {route:6.2f}s   poisoning {poison:6.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200_000)
    args = parser.parse_args()

    print(f"chainscope benchmark --- {args.rows:,} rows\n")
    store_paths(args.rows)
    print()
    where_the_time_goes()
    print()
    graph_paths()
    print()
    print("  The network, for comparison: one uncached provider call is ~1.6s")
    print("  for 55 transfers --- about 35 rows/s. See docs/why-python.md.")


if __name__ == "__main__":
    main()
