"""``chainscope graph`` --- export the fund flow around an address.

Defaults to a self-contained HTML file because that is the artefact people
actually want: something they can open, show someone, and attach to a report,
on a machine that has never installed this. The other formats exist for feeding
tools that already know how to read them.

The walk is breadth-first from a seed and is *capped*, which means almost every
interesting graph is incomplete. That is fine and unavoidable; what is not fine
is failing to say so. Nodes that were reached but never expanded are marked
frontier and drawn differently, and a walk stopped by a limit sets a flag that
reaches every export format.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...core.chainid import ChainId
from ...render.base import Renderer
from ...render.flow import to_flow_html
from ...render.graph import (
    Edge,
    Graph,
    Node,
    to_cytoscape,
    to_d3,
    to_dot,
    to_gexf,
)
from ...render.html import to_html
from ...store.sqlite import SqliteStore

__all__ = ["add_parser", "run"]

#: HTML is handled separately because it takes a title; keeping it out of this
#: table lets the rest share one signature.
_WRITERS = {
    "d3": to_d3,
    "cytoscape": to_cytoscape,
    "dot": to_dot,
    "gexf": to_gexf,
}

_SUFFIX = {
    "html": ".html",
    "flow": ".html",
    "d3": ".json",
    "cytoscape": ".json",
    "dot": ".dot",
    "gexf": ".gexf",
}


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="export the fund-flow graph around an address")
    p.add_argument("address")
    p.add_argument("--chain", "-c", default="1", help="CAIP-2 id or EVM chain number")
    p.add_argument(
        "--format",
        "-f",
        default="html",
        choices=["html", "flow", *sorted(_WRITERS)],
        help="output format",
    )
    p.add_argument("--out", "-o", type=Path, help="write here instead of stdout")
    p.add_argument("--depth", "-d", type=int, default=2, help="hops from the seed")
    p.add_argument(
        "--max-nodes",
        type=int,
        default=150,
        help="stop expanding past this many. Hitting it marks the graph truncated",
    )
    p.add_argument(
        "--per-node",
        type=int,
        default=12,
        help="strongest flows to follow per address, by value",
    )
    p.add_argument("--direction", default="out", choices=["out", "in", "both"])
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument("--title", help="heading for the HTML view")
    p.add_argument(
        "--visible-depth",
        type=int,
        help="flow view only: draw this many hops and fold the rest into the "
        "page, openable by clicking. Defaults to all of them",
    )


def _chain(raw: str) -> ChainId:
    """Parse a chain id, or refuse.

    Refusing matters: a typo that resolved to "unspecified" would be
    reinterpreted downstream as Ethereum, and the caller would get a confident
    answer about a chain they did not ask about.
    """
    text = raw.strip()
    if text.isdigit():
        return ChainId.evm(int(text))
    namespace, _, reference = text.partition(":")
    if not reference:
        raise ValueError(
            f"not a chain id: {raw!r}. Use an EVM chain number (1, 56) or a "
            f"CAIP-2 identifier (eip155:1, sui:mainnet)."
        )
    return ChainId(namespace, reference)


def _strongest(edges: list[Any], limit: int) -> list[Any]:
    """The ``limit`` most significant edges, without comparing across assets.

    Ranking by ``total_raw`` alone mixes units: raw amounts are only comparable
    within one asset, so 0.0001 of an 18-decimal token outranks 5,000 USDC at
    six decimals and consumes the budget with dust. A spoofed token minted with
    a huge supply is the adversarial version of the same thing.

    So: rank within each asset, where raw *is* the right comparison, then take
    from the assets in turn. Each asset present gets representation instead of
    one of them crowding the rest out --- which also means a stablecoin flow
    cannot be hidden by flooding an address with a worthless token.

    Prices would give a true ordering across assets. This module does not have
    them, and inventing one from raw magnitudes would be a guess wearing a
    number.
    """
    if limit <= 0 or len(edges) <= limit:
        return list(edges)

    by_asset: dict[str, list[Any]] = {}
    for edge in edges:
        by_asset.setdefault(edge.asset or "", []).append(edge)
    for group in by_asset.values():
        group.sort(key=lambda e: e.total_raw, reverse=True)

    # Assets with a larger single edge go first, so the round-robin starts from
    # the most significant flow in each. Still no cross-asset magnitude claim:
    # this only decides the order of the turns.
    order = sorted(by_asset.values(), key=lambda g: len(g), reverse=True)
    out: list[Any] = []
    for rank in range(max(len(g) for g in order)):
        for group in order:
            if rank < len(group):
                out.append(group[rank])
                if len(out) == limit:
                    return out
    return out


def run(args: argparse.Namespace, render: Renderer) -> int:
    if not args.store.exists():
        _err(f"no store at {args.store}. Run an analysis first.")
        return 1
    try:
        chain = _chain(args.chain)
    except ValueError as exc:
        _err(str(exc))
        return 2

    store = SqliteStore(args.store)
    try:
        graph = _walk(
            store,
            args.address,
            chain,
            depth=args.depth,
            max_nodes=args.max_nodes,
            per_node=args.per_node,
            direction=args.direction,
        )
    finally:
        store.close()

    if args.format == "flow":
        # Columns by hop distance rather than a spring layout: a laundering
        # chain and a five-way split are the same picture in a force-directed
        # graph, and telling them apart is the point.
        content = to_flow_html(
            graph,
            title=args.title or f"{args.address[:12]}… — flow",
            visible_depth=args.visible_depth,
        )
    elif args.format == "html":
        content = to_html(graph, title=args.title or f"{args.address[:12]}… — chainscope")
    else:
        content = _WRITERS[args.format](graph)

    if args.out:
        out = args.out
        if out.suffix == "":
            out = out.with_suffix(_SUFFIX[args.format])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        summary = graph.summary()
        print(
            f"{out}: {summary['nodes']} addresses, {summary['edges']} flows, "
            f"{summary['frontier']} on the frontier"
        )
        if summary["truncated"]:
            print(
                "  truncated: a limit stopped the walk, so this is not the "
                "whole case. Raise --max-nodes or narrow the seed."
            )
        if summary["unlabelled"]:
            print(f"  {summary['unlabelled']} addresses have no attribution")
    else:
        print(content)
    return 0


def _walk(
    store: SqliteStore,
    seed: str,
    chain: ChainId,
    *,
    depth: int,
    max_nodes: int,
    per_node: int,
    direction: str,
) -> Graph:
    """Breadth-first expansion from a seed, following the largest flows.

    Following by value rather than by count is the right default for tracing
    funds: a hundred dust transfers matter less than one large one, and a
    breadth-first walk that follows count spends its budget on noise.

    The cap is enforced by refusing to *expand* further, not by refusing to
    record. An address at the boundary still appears --- marked frontier ---
    because dropping it would hide that the walk had somewhere left to go.
    """
    graph = Graph(seeds=[f"{chain}:{seed}"])
    graph.add_node(Node(address=seed, chain=str(chain), is_seed=True, expanded=True))
    _attribute(store, graph, seed, chain)

    ways = ("out", "in") if direction == "both" else (direction,)
    frontier = [seed]
    expanded: set[str] = {seed.lower()}
    # Walking both ways reaches a transfer twice --- once from the sender's
    # outbound edges and again from the recipient's inbound ones --- and
    # add_edge folds the second sighting into the first, doubling the displayed
    # total and count. The pair is the same edge either way, so record it once.
    recorded: set[tuple[str, str, str, str]] = set()

    for _ in range(max(0, depth)):
        following: list[str] = []
        for address in frontier:
            # Marked here, at the point its edges are actually read, because
            # that is what the flag claims. Marking the ring after collecting it
            # meant the *last* ring was flagged expanded and then the loop
            # ended, so an address with five hundred onward transfers rendered
            # as a leaf --- indistinguishable from one that genuinely had
            # nowhere to go, and `graph.frontier()` came back empty on a walk
            # that had stopped early.
            graph.add_node(Node(address=address, chain=str(chain), expanded=True))
            for way in ways:
                edges = store.edges(address, chain, direction=way)
                kept = _strongest(edges, per_node)
                if len(kept) < len(edges):
                    # The counterparties beyond the cap are not merely unshown;
                    # nothing recorded that they exist. Without this the export
                    # presents five of twenty as the whole picture, which is the
                    # failure this module's docstring names.
                    graph.truncated = True
                for edge in kept:
                    other = edge.recipient if way == "out" else edge.sender
                    if not other:
                        continue
                    candidate = Edge(
                        source=edge.sender,
                        target=edge.recipient,
                        chain=str(chain),
                        symbol=edge.symbol,
                        decimals=edge.decimals,
                        total_raw=edge.total_raw,
                        transfer_count=edge.transfer_count,
                        asset=edge.asset,
                        first_seen=int(edge.first_seen.timestamp())
                        if edge.first_seen
                        else None,
                        last_seen=int(edge.last_seen.timestamp()) if edge.last_seen else None,
                    )
                    if candidate.key not in recorded:
                        recorded.add(candidate.key)
                        graph.add_edge(candidate)
                    graph.add_node(Node(address=other, chain=str(chain)))
                    _attribute(store, graph, other, chain)

                    if other.lower() in expanded:
                        continue
                    if len(graph.nodes) >= max_nodes:
                        # Recorded, not expanded, and the graph now says so.
                        graph.truncated = True
                        continue
                    following.append(other)
                    # This set means "queued", which is deliberately *not* the
                    # same as the node's `expanded` flag: the last queue never
                    # gets processed, and conflating the two is what produced
                    # the false leaves above.
                    expanded.add(other.lower())
        if not following:
            break
        frontier = following

    return graph


def _attribute(store: SqliteStore, graph: Graph, address: str, chain: ChainId) -> None:
    """Attach the claims that apply to *this* chain.

    An address string is not unique across chains --- the same twenty bytes
    exist on Ethereum, BSC, and everything else EVM --- so a claim recorded
    against BSC says nothing about the Ethereum address that happens to share
    its hex. Attaching it anyway put "PancakeSwap" on an Ethereum node.

    Claims with no chain are kept: those are deliberate assertions about the
    address wherever it appears, which is how sanctions lists are published.
    """
    for claim in store.attributions(address):
        if claim.chain is None or claim.chain == chain:
            graph.attribute(address, str(chain), claim)
