"""Fund-flow graphs: nodes, edges, and the confidence attached to each.

Written before any user interface, deliberately. A visual layer that cannot
show where a label came from would contradict everything the rest of this
project is built around --- and once a diagram exists, the pressure is always
to draw first and attach provenance later, which never happens.

So the graph is a data structure, not a picture. It carries attribution and
confidence on every node, marks the frontier explicitly, and exports to formats
that existing tools already read. Whatever draws it is then replaceable, and
several things can draw the same graph without agreeing on anything but this.

**Three properties that a fund-flow view gets wrong if they are not designed
in:**

*Edges are aggregates, not transfers.* One counterparty with four hundred
transfers is one edge with a total and a count. Drawing four hundred lines is
both unreadable and, at any real case size, unrenderable.

*The frontier is not the boundary of the world.* An address that was seen but
never expanded looks identical to one that was expanded and had no
counterparties, unless the graph says which. A diagram that does not
distinguish them silently overstates its own coverage --- which is the visual
form of the failure this project exists to prevent.

*Assets do not add up.* Two edges denominated in different tokens cannot be
summed into one number, and a graph that does so produces a total that means
nothing. Edges are per-asset; anything that wants a single width has to say
what it converted with.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..chains import address_key
from ..core.attribution import Attribution, Category, Confidence

__all__ = [
    "Edge",
    "Graph",
    "Node",
    "to_cytoscape",
    "to_d3",
    "to_dot",
    "to_gexf",
]


@dataclass(frozen=True, slots=True)
class Node:
    """One address in the graph."""

    address: str
    chain: str

    label: str = ""
    """Human name, if attribution supplied one. Empty is honest --- an
    unlabelled address should look unlabelled, not be given its own truncated
    hex as a name and then read as identified."""

    category: str = ""
    confidence: int = -1
    """:class:`~chainscope.core.attribution.Confidence` as an int, or -1 for
    "no claim". Distinct from ``SPECULATIVE`` (0), which *is* a claim."""

    source: str = ""
    expanded: bool = False
    """Whether this address's counterparties were fetched. False means the
    graph stops here because nobody looked further, not because there is
    nothing further."""

    is_seed: bool = False
    balance_raw: int | None = None
    tags: tuple[str, ...] = ()

    @property
    def is_frontier(self) -> bool:
        return not self.expanded

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        return (
            f"{self.address[:8]}…{self.address[-6:]}"
            if len(self.address) > 16
            else self.address
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": f"{self.chain}:{self.address}",
            "address": self.address,
            "chain": self.chain,
            "label": self.label,
            "display": self.display,
            "category": self.category,
            "confidence": self.confidence,
            "source": self.source,
            "expanded": self.expanded,
            "frontier": self.is_frontier,
            "seed": self.is_seed,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class Edge:
    """Aggregate flow between two addresses in one asset."""

    source: str
    target: str
    chain: str
    symbol: str
    total_raw: int
    decimals: int = 18
    transfer_count: int = 1
    asset: str | None = None
    first_seen: int | None = None
    last_seen: int | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.chain, self.source, self.target, self.asset or self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": f"{self.chain}:{self.source}",
            "target": f"{self.chain}:{self.target}",
            "chain": self.chain,
            "symbol": self.symbol,
            # A string, not a number. JSON numbers are IEEE 754 doubles in
            # every browser that will read this, and 10 ETH already exceeds
            # what one represents exactly -- the value would arrive silently
            # rounded.
            "total_raw": str(self.total_raw),
            "decimals": self.decimals,
            "transfers": self.transfer_count,
            "asset": self.asset,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class Graph:
    """Nodes and aggregated edges, with provenance kept attached."""

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str], Edge] = field(default_factory=dict)
    seeds: list[str] = field(default_factory=list)
    truncated: bool = False
    """Set when a cap stopped the walk. A graph that hit a limit and does not
    say so reads as a complete picture of a case."""

    note: str = ""

    # ------------------------------------------------------------------ adding

    def add_node(self, node: Node) -> Node:
        """Insert, or merge into an existing node.

        Merging keeps the *better* attribution rather than the newer one. A
        second sighting of an address through a lower-confidence path must not
        downgrade a claim already made on stronger evidence.
        """
        key = f"{node.chain}:{node.address}"
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = node
            return node

        # The attribution fields move together, from whichever claim is
        # stronger. Merging them independently produces a composite that nobody
        # asserted: a HIGH "Binance / cex" arriving over a LOW "" / mixer" gave
        # label=Binance, category=mixer, confidence=HIGH --- an exchange
        # confidently labelled a mixer, on the strength of two claims that each
        # said something else. Ties keep the existing claim, so replaying the
        # same data twice does not shuffle the answer.
        winner = node if node.confidence > existing.confidence else existing
        loser = existing if winner is node else node

        # Fall back to the weaker claim only where the stronger one is silent
        # --- an unlabelled HIGH-confidence sighting should not erase a label
        # somebody actually recorded.
        label = winner.label or loser.label
        category = winner.category or loser.category

        # The confidence of the *retained fields*, which is not the same as the
        # winner's. It used to be the winner's whenever the winner supplied any
        # field at all --- so a HIGH sighting carrying only a category, merged
        # with a SPECULATIVE claim carrying a label, rendered "Probably Lazarus"
        # at HIGH. The flow view drops its `?` hedge at HIGH, so a hunch was
        # drawn as an identification: the precise confusion this package exists
        # to prevent, produced by the merge rather than by any source.
        #
        # So: the weakest confidence among the claims that actually contributed
        # a field. A mixed-source node cannot be reported above the confidence
        # of its own weakest contributor.
        contributors = [
            source.confidence
            for source, field_ in ((winner, winner.label), (loser, loser.label))
            if field_ and field_ == label
        ] + [
            source.confidence
            for source, field_ in ((winner, winner.category), (loser, loser.category))
            if field_ and field_ == category
        ]
        merged = Node(
            address=existing.address,
            chain=existing.chain,
            label=label,
            category=category,
            # Nothing claimed by either: keep the winner's, since there is no
            # field whose strength could be overstated.
            confidence=min(contributors) if contributors else winner.confidence,
            source=winner.source or loser.source,
            # These are facts about what was fetched rather than claims about
            # what something is, so they merge independently and always widen.
            expanded=existing.expanded or node.expanded,
            is_seed=existing.is_seed or node.is_seed,
            balance_raw=existing.balance_raw
            if existing.balance_raw is not None
            else node.balance_raw,
            tags=tuple(sorted(set(existing.tags) | set(node.tags))),
        )
        self.nodes[key] = merged
        return merged

    def add_edge(self, edge: Edge) -> Edge:
        """Insert, or fold into the matching edge.

        Edges are keyed by (chain, source, target, asset). Folding two USDC
        edges together is correct; folding a USDC edge into an ETH one would
        produce a number denominated in nothing.
        """
        existing = self.edges.get(edge.key)
        if existing is None:
            self.edges[edge.key] = edge
            return edge

        folded = Edge(
            source=existing.source,
            target=existing.target,
            chain=existing.chain,
            symbol=existing.symbol or edge.symbol,
            total_raw=existing.total_raw + edge.total_raw,
            decimals=existing.decimals,
            transfer_count=existing.transfer_count + edge.transfer_count,
            asset=existing.asset or edge.asset,
            first_seen=_min(existing.first_seen, edge.first_seen),
            last_seen=_max(existing.last_seen, edge.last_seen),
        )
        self.edges[edge.key] = folded
        return folded

    def attribute(self, address: str, chain: str, attribution: Attribution) -> None:
        """Attach a claim to a node, creating it if needed."""
        self.add_node(
            Node(
                address=address,
                chain=chain,
                label=attribution.label,
                category=str(getattr(attribution.category, "value", attribution.category)),
                confidence=int(attribution.confidence),
                source=attribution.source,
            )
        )

    # ------------------------------------------------------------------ reading

    def frontier(self) -> list[Node]:
        """Nodes seen but never expanded --- the honest edge of the case."""
        return [n for n in self.nodes.values() if n.is_frontier]

    def by_category(self, category: Category | str) -> list[Node]:
        want = str(getattr(category, "value", category))
        return [n for n in self.nodes.values() if n.category == want]

    def sanctioned(self) -> list[Node]:
        return self.by_category(Category.SANCTIONED)

    def unlabelled(self) -> list[Node]:
        return [n for n in self.nodes.values() if not n.label]

    def summary(self) -> dict[str, Any]:
        """What a reader needs before trusting the picture."""
        assets = sorted({e.symbol for e in self.edges.values() if e.symbol})
        confident = [n for n in self.nodes.values() if n.confidence >= Confidence.HIGH]
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "transfers": sum(e.transfer_count for e in self.edges.values()),
            "assets": assets,
            "seeds": list(self.seeds),
            "frontier": len(self.frontier()),
            "unlabelled": len(self.unlabelled()),
            "high_confidence": len(confident),
            "sanctioned": len(self.sanctioned()),
            "truncated": self.truncated,
            "note": self.note,
        }

    def totals_by_asset(self) -> dict[str, int]:
        """Exact per-asset totals. Never one combined number --- see the module
        docstring on why summing across assets produces a meaningless figure."""
        out: dict[str, int] = {}
        for edge in self.edges.values():
            out[edge.symbol] = out.get(edge.symbol, 0) + edge.total_raw
        return out

    def __len__(self) -> int:
        return len(self.nodes)

    def __repr__(self) -> str:
        flag = " truncated" if self.truncated else ""
        return f"<Graph {len(self.nodes)} nodes {len(self.edges)} edges{flag}>"


def _min(a: int | None, b: int | None) -> int | None:
    return min(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None


def _max(a: int | None, b: int | None) -> int | None:
    return max(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None


# --------------------------------------------------------------------- builders


def graph_from_flows(
    seed: str,
    chain: str,
    flows: Iterable[Any],
    *,
    expanded: Sequence[str] = (),
    truncated: bool = False,
) -> Graph:
    """Build a graph from :class:`~chainscope.store.analytics.Flow` records.

    ``expanded`` names the addresses whose counterparties were actually
    fetched. Anything not in it is frontier, which the export marks --- the
    alternative is a picture that presents "we stopped here" as "nothing
    further exists".
    """
    graph = Graph(seeds=[f"{chain}:{seed}"], truncated=truncated)
    # Chain-aware: lowercasing here made every Solana, Sui and Bitcoin address
    # miss the expanded set, so every node was drawn as frontier --- the picture
    # said "nobody looked past here" about addresses that had been walked.
    known = {address_key(chain, a) for a in expanded} | {address_key(chain, seed)}

    graph.add_node(Node(address=seed, chain=chain, is_seed=True, expanded=True))
    for flow in flows:
        for address in (flow.sender, flow.recipient):
            graph.add_node(
                Node(
                    address=address,
                    chain=chain,
                    expanded=address_key(chain, address) in known,
                )
            )
        graph.add_edge(
            Edge(
                source=flow.sender,
                target=flow.recipient,
                chain=chain,
                symbol=flow.symbol,
                total_raw=flow.total_raw,
                transfer_count=flow.transfer_count,
                asset=flow.asset,
                first_seen=flow.first_seen,
                last_seen=flow.last_seen,
            )
        )
    return graph


# --------------------------------------------------------------------- exports


def to_d3(graph: Graph) -> str:
    """JSON in the shape d3-force and most JS graph libraries expect."""
    return json.dumps(
        {
            "nodes": [n.to_dict() for n in graph.nodes.values()],
            "links": [e.to_dict() for e in graph.edges.values()],
            "summary": graph.summary(),
        },
        indent=2,
    )


def to_cytoscape(graph: Graph) -> str:
    """Cytoscape.js elements. Reads in Cytoscape Desktop too."""
    elements = [{"data": n.to_dict()} for n in graph.nodes.values()]
    elements += [{"data": e.to_dict()} for e in graph.edges.values()]
    return json.dumps({"elements": elements, "summary": graph.summary()}, indent=2)


def to_gexf(graph: Graph) -> str:
    """GEXF, for Gephi --- still the tool investigators reach for.

    Every value below goes through ``quoteattr``, which supplies the surrounding
    quotes itself. ``escape`` alone is not enough: it handles ``&``, ``<`` and
    ``>`` but leaves quotation marks intact, and a label containing one lands
    inside a double-quoted attribute and ends it early. Labels are arbitrary
    user text --- an exchange name with a quoted alias is entirely ordinary ---
    so this is a matter of when, not whether.
    """
    from xml.sax.saxutils import quoteattr

    ids = {key: str(i) for i, key in enumerate(graph.nodes)}
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gexf xmlns="http://www.gexf.net/1.2draft" version="1.2">',
        '  <graph mode="static" defaultedgetype="directed">',
        '    <attributes class="node">',
        '      <attribute id="0" title="category" type="string"/>',
        '      <attribute id="1" title="confidence" type="integer"/>',
        '      <attribute id="2" title="frontier" type="boolean"/>',
        '      <attribute id="3" title="source" type="string"/>',
        "    </attributes>",
        "    <nodes>",
    ]
    for key, node in graph.nodes.items():
        lines += [
            f"      <node id={quoteattr(ids[key])} label={quoteattr(node.display)}>",
            "        <attvalues>",
            f'          <attvalue for="0" value={quoteattr(node.category)}/>',
            f'          <attvalue for="1" value="{node.confidence}"/>',
            f'          <attvalue for="2" value="{str(node.is_frontier).lower()}"/>',
            f'          <attvalue for="3" value={quoteattr(node.source)}/>',
            "        </attvalues>",
            "      </node>",
        ]
    lines += ["    </nodes>", "    <edges>"]
    for i, edge in enumerate(graph.edges.values()):
        src = ids.get(f"{edge.chain}:{edge.source}")
        tgt = ids.get(f"{edge.chain}:{edge.target}")
        if src is None or tgt is None:
            # An edge with an endpoint nobody added would emit a dangling id
            # and make the file unreadable to Gephi.
            continue
        lines.append(
            f'      <edge id="{i}" source={quoteattr(src)} target={quoteattr(tgt)} '
            f'weight="{edge.transfer_count}" label={quoteattr(edge.symbol)}/>'
        )
    lines += ["    </edges>", "  </graph>", "</gexf>"]
    return "\n".join(lines)


def _dot(value: str) -> str:
    """Quote a value for DOT.

    Same reasoning as ``quoteattr`` in :func:`to_gexf`: labels are arbitrary
    user text, and one containing a quotation mark would close the attribute
    early and produce a file Graphviz refuses. The escape ``\\n`` is left alone
    because the label builder uses it deliberately for line breaks.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\\\\n", "\\n")
    return f'"{escaped}"'


def to_dot(graph: Graph) -> str:
    """Graphviz. The one format that renders with no install and no browser.

    Frontier nodes are drawn dashed. That is not decoration: the distinction
    between "expanded and empty" and "never looked at" is the difference
    between a finding and an assumption.
    """
    palette = {
        "sanctioned": "#c62828",
        "mixer": "#ad1457",
        "cex": "#1565c0",
        "dex": "#2e7d32",
        "bridge": "#6a1b9a",
        "illicit": "#e65100",
    }
    lines = ["digraph flows {", "  rankdir=LR;", '  node [shape=box style="rounded,filled"];']
    for key, node in graph.nodes.items():
        colour = palette.get(node.category, "#eceff1")
        font = "white" if node.category in palette else "black"
        style = "rounded,filled,dashed" if node.is_frontier else "rounded,filled"
        if node.is_seed:
            style += ",bold"
        label = node.display
        if node.category:
            label += f"\\n[{node.category}]"
        lines.append(
            f"  {_dot(key)} [label={_dot(label)} fillcolor={_dot(colour)} "
            f"fontcolor={_dot(font)} style={_dot(style)}];"
        )
    for edge in graph.edges.values():
        src, tgt = f"{edge.chain}:{edge.source}", f"{edge.chain}:{edge.target}"
        label = f"{edge.symbol} x{edge.transfer_count}"
        lines.append(f"  {_dot(src)} -> {_dot(tgt)} [label={_dot(label)}];")
    if graph.truncated:
        lines.append('  labelloc="b"; label="TRUNCATED --- this graph is not complete";')
    lines.append("}")
    return "\n".join(lines)
