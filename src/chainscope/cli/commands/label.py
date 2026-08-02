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


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...core.chainid import resolve as resolve_chain

    resolver = Resolver()
    if args.sanctions:
        resolver.add(OfacSource(args.sanctions))
    if args.nametags:
        resolver.add(ExplorerDumpSource(args.nametags))
    if args.local:
        resolver.add(LocalSource(args.local))

    if not resolver.sources:
        print("no sources configured; pass --sanctions, --nametags, or --local")
        return 2

    chain = resolve_chain(args.chain) if args.chain else None
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
