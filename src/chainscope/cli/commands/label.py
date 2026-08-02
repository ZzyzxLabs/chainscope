"""Look up what is known about an address."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ...attribution.resolver import Resolver
from ...attribution.sources.etherscan_dump import ExplorerDumpSource
from ...attribution.sources.local import LocalSource
from ...attribution.sources.ofac import OfacSource
from ...render.base import Renderer, qualify_entity

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="resolve an address against attribution sources")
    p.add_argument("address")
    p.add_argument("--chain", "-c")
    p.add_argument("--sanctions", type=Path, help="OFAC extract (JSON)")
    p.add_argument("--nametags", type=Path, help="explorer nametag dump (JSON)")
    p.add_argument("--local", type=Path, help="your own label file (JSON)")
    p.add_argument(
        "--store",
        type=Path,
        default=Path(".chainscope/store.db"),
        help="the case store, consulted first. --no-store to skip it",
    )
    p.add_argument("--no-store", action="store_true", help="do not read the case store")


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...core.chainid import resolve as resolve_chain

    resolver = Resolver()

    # The store comes first, and this was missing.
    #
    # Somebody tags an address, then runs `chainscope label` on it --- the most
    # obvious next command --- and was told "no sources configured". Their own
    # label was sitting in the store the whole time. A tool that cannot find
    # what it just recorded reads as broken, and reasonably so.
    store_claims: list[Any] = []
    if not args.no_store and args.store.exists():
        from ...store.sqlite import SqliteStore

        store = SqliteStore(args.store)
        try:
            chain_filter = resolve_chain(args.chain) if args.chain else None
            store_claims = [
                c
                for c in store.attributions(args.address)
                # A claim with no chain applies everywhere; one scoped to
                # another chain says nothing about this address here.
                if chain_filter is None or c.chain is None or c.chain == chain_filter
            ]
        finally:
            store.close()

    if args.sanctions:
        resolver.add(OfacSource(args.sanctions))
    if args.nametags:
        resolver.add(ExplorerDumpSource(args.nametags))
    if args.local:
        resolver.add(LocalSource(args.local))

    if not resolver.sources and not store_claims:
        if args.no_store or not args.store.exists():
            print(
                f"nothing to search: no case store at {args.store} and no "
                f"external source configured. Pass --sanctions, --nametags, or "
                f"--local, or record a label with `chainscope tag`."
            )
        else:
            print(
                f"{args.address} has no attribution in {args.store}, and no "
                f"external source was configured. That is not evidence it is "
                f"unlabelled --- only that nothing here has labelled it."
            )
        return 2

    chain = resolve_chain(args.chain) if args.chain else None
    if store_claims:
        print(f"from the case store ({args.store}):")
        for claim in store_claims:
            print(
                f"  {claim.label}  [{claim.category.value}, "
                f"{claim.confidence.name.lower()}, source: {claim.source}]"
            )
            if claim.rationale:
                print(f"      {claim.rationale}")
        if not resolver.sources:
            return 0
        print()

    res = resolver.resolve(args.address, chain)

    print(f"{args.address}\n  {qualify_entity(res.entity)}")

    sanctioned = res.is_sanctioned
    verdict = {True: "yes", False: "no", None: "UNKNOWN -- lookup incomplete"}[sanctioned]
    print(f"  sanctioned: {verdict}")

    for w in res.warnings():
        print(f"  ! {w}")

    if res.entity and len(res.entity.all_claims) > 1:
        print("\n  all claims")
        for claim in res.entity.all_claims:
            print(f"    - {claim}")

    print("\n  sources consulted: " + (", ".join(res.consulted) or "none"))
    # Exit non-zero on a sanctions hit *or* an unreliable lookup, so a shell
    # pipeline treats "could not check" as a problem rather than a pass.
    return 0 if sanctioned is False else 1
