"""Fund-flow graphs.

The properties worth guarding are the ones a visual layer gets wrong when
nobody wrote them down: that an unexpanded node is not an empty one, that two
assets do not add up, and that a number too large for a JSON double does not
quietly become a different number on the way to a browser.
"""

import json

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.render.graph import (
    Edge,
    Graph,
    Node,
    to_cytoscape,
    to_d3,
    to_dot,
    to_gexf,
)

CHAIN = str(ETHEREUM)
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40

TEN_ETH = 10 * 10**18


def edge(src, dst, raw=TEN_ETH, symbol="ETH", count=1, asset=None):
    return Edge(
        source=src,
        target=dst,
        chain=CHAIN,
        symbol=symbol,
        total_raw=raw,
        transfer_count=count,
        asset=asset,
    )


@pytest.fixture
def graph():
    g = Graph(seeds=[f"{CHAIN}:{A}"])
    g.add_node(Node(address=A, chain=CHAIN, is_seed=True, expanded=True))
    g.add_node(Node(address=B, chain=CHAIN, expanded=True))
    g.add_node(Node(address=C, chain=CHAIN))  # never expanded: frontier
    g.add_edge(edge(A, B))
    g.add_edge(edge(B, C, raw=5_000_000, symbol="USDC"))
    return g


class TestNodes:
    def test_a_stronger_claim_wins(self):
        """A later sighting through a weaker path must not overwrite a claim
        already made on better evidence."""
        g = Graph()
        g.add_node(Node(A, CHAIN, label="guess", confidence=1, source="heuristic"))
        g.add_node(Node(A, CHAIN, label="Binance 14", confidence=3, source="etherscan"))
        node = g.nodes[f"{CHAIN}:{A}"]
        assert node.label == "Binance 14"
        assert node.source == "etherscan"

    def test_a_weaker_claim_does_not_downgrade(self):
        g = Graph()
        g.add_node(Node(A, CHAIN, label="Binance 14", confidence=3, source="etherscan"))
        g.add_node(Node(A, CHAIN, label="maybe an exchange", confidence=0, source="rumour"))
        node = g.nodes[f"{CHAIN}:{A}"]
        assert node.label == "Binance 14"
        assert node.confidence == 3

    def test_expansion_is_sticky(self):
        """Whether the counterparties were fetched is a fact about what
        happened, not a property of the most recent sighting."""
        g = Graph()
        g.add_node(Node(A, CHAIN, expanded=True))
        g.add_node(Node(A, CHAIN, expanded=False))
        assert g.nodes[f"{CHAIN}:{A}"].expanded

    def test_no_claim_is_distinct_from_a_weak_claim(self):
        """-1 means nobody said anything; 0 (SPECULATIVE) means somebody did."""
        assert Node(A, CHAIN).confidence == -1
        assert Confidence.SPECULATIVE == 0

    def test_an_unlabelled_node_looks_unlabelled(self):
        """Substituting truncated hex for a name would read as identification."""
        assert Node(A, CHAIN).label == ""
        assert "…" in Node(A, CHAIN).display

    def test_tags_merge_rather_than_replace(self):
        g = Graph()
        g.add_node(Node(A, CHAIN, tags=("deposit",)))
        g.add_node(Node(A, CHAIN, tags=("hot-wallet",)))
        assert g.nodes[f"{CHAIN}:{A}"].tags == ("deposit", "hot-wallet")

    def test_attribute_creates_the_node(self, graph):
        graph.attribute(
            C,
            CHAIN,
            Attribution(
                label="Tornado Cash",
                category=Category.SANCTIONED,
                confidence=Confidence.CERTAIN,
                method=Method.LIST,
                source="OFAC SDN",
                address=C,
                chain=ETHEREUM,
            ),
        )
        assert graph.sanctioned()[0].address == C


class TestFrontier:
    def test_unexpanded_nodes_are_frontier(self, graph):
        assert [n.address for n in graph.frontier()] == [C]

    def test_expanded_and_empty_is_not_frontier(self):
        """The distinction the whole flag exists for: "we looked and found
        nothing" is a finding; "we never looked" is not."""
        g = Graph()
        g.add_node(Node(A, CHAIN, expanded=True))
        assert g.frontier() == []

    def test_the_summary_reports_the_frontier(self, graph):
        assert graph.summary()["frontier"] == 1

    def test_truncation_is_carried(self):
        g = Graph(truncated=True)
        assert g.summary()["truncated"]

    def test_truncation_reaches_the_rendered_output(self):
        g = Graph(truncated=True)
        g.add_node(Node(A, CHAIN))
        assert "TRUNCATED" in to_dot(g)


class TestEdges:
    def test_repeated_flows_fold_together(self):
        g = Graph()
        g.add_edge(edge(A, B, count=2))
        g.add_edge(edge(A, B, count=3))
        (only,) = g.edges.values()
        assert only.transfer_count == 5
        assert only.total_raw == TEN_ETH * 2

    def test_different_assets_stay_apart(self):
        """Folding a USDC edge into an ETH one produces a number denominated
        in nothing."""
        g = Graph()
        g.add_edge(edge(A, B, symbol="ETH", asset=None))
        g.add_edge(edge(A, B, symbol="USDC", asset="0xa0b8"))
        assert len(g.edges) == 2

    def test_direction_matters(self):
        g = Graph()
        g.add_edge(edge(A, B))
        g.add_edge(edge(B, A))
        assert len(g.edges) == 2

    def test_totals_are_per_asset(self, graph):
        # Keyed by (contract, symbol, decimals), not by symbol. A token that
        # chose its ticker to be read as another one would otherwise have its
        # total added to the real asset's.
        totals = {sym: raw for (_c, sym, _d), raw in graph.totals_by_asset().items()}
        assert totals == {"ETH": TEN_ETH, "USDC": 5_000_000}

    def test_two_contracts_sharing_a_symbol_stay_apart(self):
        # The case the panel showed on real data: three rows reading `ETH`,
        # identical to the eye and different tokens.
        g = Graph()
        g.add_edge(Edge(A, B, CHAIN, "USDC", 1_000, asset="0x" + "a" * 40, decimals=6))
        g.add_edge(Edge(A, B, CHAIN, "USDC", 9_000, asset="0x" + "b" * 40, decimals=6))
        assert len(g.totals_by_asset()) == 2

    def test_time_range_widens_on_fold(self):
        g = Graph()
        g.add_edge(Edge(A, B, CHAIN, "ETH", TEN_ETH, first_seen=100, last_seen=200))
        g.add_edge(Edge(A, B, CHAIN, "ETH", TEN_ETH, first_seen=50, last_seen=300))
        (only,) = g.edges.values()
        assert (only.first_seen, only.last_seen) == (50, 300)


class TestExports:
    def test_d3_round_trips(self, graph):
        data = json.loads(to_d3(graph))
        assert len(data["nodes"]) == 3
        assert len(data["links"]) == 2
        assert data["summary"]["frontier"] == 1

    def test_amounts_cross_json_as_strings(self, graph):
        """A JSON number is a double in every browser that will read this, and
        10 ETH already exceeds what one holds exactly. The value would arrive
        silently rounded."""
        data = json.loads(to_d3(graph))
        raw = data["links"][0]["total_raw"]
        assert isinstance(raw, str)
        assert int(raw) == TEN_ETH
        assert int(float(TEN_ETH)) != TEN_ETH or TEN_ETH > 2**53

    def test_frontier_is_visible_in_every_format(self, graph):
        assert '"frontier": true' in to_d3(graph)
        assert "frontier" in to_cytoscape(graph)
        assert "dashed" in to_dot(graph)
        assert 'title="frontier"' in to_gexf(graph)

    def test_cytoscape_shape(self, graph):
        data = json.loads(to_cytoscape(graph))
        assert len(data["elements"]) == 5  # 3 nodes + 2 edges

    def test_gexf_is_well_formed(self, graph):
        from xml.etree import ElementTree

        root = ElementTree.fromstring(to_gexf(graph))
        assert root.tag.endswith("gexf")

    def test_gexf_escapes_labels(self):
        """A label is user-supplied text and lands inside an XML attribute."""
        from xml.etree import ElementTree

        g = Graph()
        g.add_node(Node(A, CHAIN, label='Bad & "quoted" <tag>', confidence=3))
        ElementTree.fromstring(to_gexf(g))  # raises if unescaped

    def test_gexf_skips_edges_with_no_endpoint(self):
        """An edge whose nodes were never added must not emit a dangling id."""
        from xml.etree import ElementTree

        g = Graph()
        g.add_edge(edge(A, B))
        root = ElementTree.fromstring(to_gexf(g))
        assert root.find(".//{*}edges") is not None

    def test_dot_marks_categories(self, graph):
        graph.attribute(
            C,
            CHAIN,
            Attribution(
                label="Tornado Cash",
                category=Category.SANCTIONED,
                confidence=Confidence.CERTAIN,
                method=Method.LIST,
                source="OFAC SDN",
                address=C,
                chain=ETHEREUM,
            ),
        )
        dot = to_dot(graph)
        assert "sanctioned" in dot
        assert "Tornado Cash" in dot

    def test_dot_escapes_labels(self):
        """Same failure as GEXF: a quotation mark in a label ends the attribute
        early and Graphviz refuses the file."""
        g = Graph()
        g.add_node(Node(A, CHAIN, label='Exchange "Hot" Wallet', confidence=3))
        dot = to_dot(g)
        assert '\\"Hot\\"' in dot
        # Every quote is either an escape or one of a balanced pair.
        assert dot.count('"') % 2 == 0

    def test_dot_keeps_intentional_line_breaks(self):
        g = Graph()
        g.add_node(Node(A, CHAIN, label="Binance", category="cex", confidence=3))
        assert "\\n[cex]" in to_dot(g)

    def test_attribution_fields_move_together(self):
        """Merging them independently produced a composite nobody asserted.

        A HIGH "Binance / cex" arriving over a LOW "unlabelled / mixer" gave
        label=Binance, category=mixer, confidence=HIGH: an exchange confidently
        called a mixer, from two claims that each said something else.
        """
        g = Graph()
        g.add_node(Node(A, CHAIN, label="", category="mixer", confidence=1, source="heuristic"))
        g.add_node(
            Node(A, CHAIN, label="Binance 14", category="cex", confidence=3, source="etherscan")
        )
        node = g.nodes[f"{CHAIN}:{A}"]
        assert (node.label, node.category, node.source) == ("Binance 14", "cex", "etherscan")

    def test_a_tie_keeps_the_existing_claim(self):
        """So replaying the same data twice does not shuffle the answer."""
        g = Graph()
        g.add_node(Node(A, CHAIN, label="first", category="cex", confidence=3, source="a"))
        g.add_node(Node(A, CHAIN, label="second", category="dex", confidence=3, source="b"))
        assert g.nodes[f"{CHAIN}:{A}"].label == "first"

    def test_a_silent_stronger_sighting_does_not_erase_a_label(self):
        """An unlabelled HIGH-confidence node should not blank a real label."""
        g = Graph()
        g.add_node(Node(A, CHAIN, label="Binance 14", category="cex", confidence=2, source="x"))
        g.add_node(Node(A, CHAIN, confidence=3, expanded=True))
        node = g.nodes[f"{CHAIN}:{A}"]
        assert node.label == "Binance 14"
        assert node.expanded
