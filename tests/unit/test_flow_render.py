"""The layered flow view: layout, units, and the honesty the graph carries."""

from __future__ import annotations

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.render.flow import layer_nodes, to_flow_html
from chainscope.render.graph import Edge, Graph, Node

A, B, C, D = ("0x" + c * 40 for c in "abcd")


def chain_graph():
    """A → B → C → D: a four-hop path, which is the shape a spring layout
    cannot distinguish from a four-way split."""
    g = Graph(seeds=[f"{ETHEREUM}:{A}"])
    g.add_node(Node(address=A, chain=str(ETHEREUM), is_seed=True, expanded=True))
    for src, dst in ((A, B), (B, C), (C, D)):
        g.add_node(Node(address=dst, chain=str(ETHEREUM), expanded=dst != D))
        g.add_edge(
            Edge(
                source=src,
                target=dst,
                chain=str(ETHEREUM),
                symbol="ETH",
                decimals=18,
                total_raw=10**19,
                transfer_count=1,
            )
        )
    return g


def split_graph():
    g = Graph(seeds=[f"{ETHEREUM}:{A}"])
    g.add_node(Node(address=A, chain=str(ETHEREUM), is_seed=True, expanded=True))
    for dst in (B, C, D):
        g.add_node(Node(address=dst, chain=str(ETHEREUM), expanded=True))
        g.add_edge(
            Edge(
                source=A,
                target=dst,
                chain=str(ETHEREUM),
                symbol="ETH",
                decimals=18,
                total_raw=10**19,
                transfer_count=1,
            )
        )
    return g


class TestLayout:
    def test_a_chain_occupies_one_column_per_hop(self):
        d = layer_nodes(chain_graph())
        assert [d[x] for x in (A, B, C, D)] == [0, 1, 2, 3]

    def test_a_split_puts_everything_in_column_one(self):
        """The distinction the whole layout exists for."""
        d = layer_nodes(split_graph())
        assert [d[x] for x in (B, C, D)] == [1, 1, 1]

    def test_an_unreachable_node_is_placed_not_dropped(self):
        """Usually an inbound counterparty. Dropping it shows an address that
        only ever sent."""
        g = chain_graph()
        g.add_node(Node(address="0x" + "e" * 40, chain=str(ETHEREUM)))
        assert ("0x" + "e" * 40) in layer_nodes(g)

    def test_a_cycle_terminates(self):
        g = chain_graph()
        g.add_edge(
            Edge(
                source=D,
                target=A,
                chain=str(ETHEREUM),
                symbol="ETH",
                decimals=18,
                total_raw=1,
                transfer_count=1,
            )
        )
        assert layer_nodes(g)[A] == 0


class TestTheHtmlCarriesTheHonesty:
    def test_frontier_count_reaches_the_page(self):
        html = to_flow_html(chain_graph())
        assert '"frontier"' in html
        assert "never expanded" in html

    def test_truncation_reaches_the_page(self):
        g = chain_graph()
        g.truncated = True
        assert '"truncated": true' in to_flow_html(g)

    def test_amounts_are_strings_not_numbers(self):
        """10 ETH is 1e19 wei, past 2^53. A JSON number would round it."""
        assert '"raw": "10000000000000000000"' in to_flow_html(chain_graph())

    def test_it_is_self_contained(self):
        html = to_flow_html(chain_graph())
        assert "http://" not in html.replace('"http://www.w3.org/2000/svg"', "")
        assert "https://" not in html
        assert "<script src" not in html

    def test_a_hostile_label_cannot_close_the_script(self):
        g = chain_graph()
        g.attribute(
            B,
            str(ETHEREUM),
            Attribution(
                label="</script><img src=x onerror=alert(1)>",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LIST,
                source="untrusted import",
                address=B,
                chain=ETHEREUM,
            ),
        )
        html = to_flow_html(g)
        assert "</script><img" not in html
        assert "\\u003c/script" in html or "\\u003c" in html

    def test_a_hostile_title_is_escaped(self):
        html = to_flow_html(chain_graph(), title="<img src=x onerror=alert(1)>")
        assert "<img src=x" not in html

    def test_confidence_travels_with_the_label(self):
        g = chain_graph()
        g.attribute(
            B,
            str(ETHEREUM),
            Attribution(
                label="Binance",
                category=Category.CEX,
                confidence=Confidence.SPECULATIVE,
                method=Method.HEURISTIC,
                source="guess",
                address=B,
                chain=ETHEREUM,
                rationale="hunch",
            ),
        )
        html = to_flow_html(g)
        assert '"confidence": 0' in html
        assert "not an identification" in html


class TestAssetScales:
    def test_each_asset_gets_its_own_maximum(self):
        """One width across mixed assets is a number that means nothing."""
        g = chain_graph()
        g.add_edge(
            Edge(
                source=A,
                target=C,
                chain=str(ETHEREUM),
                symbol="USDC",
                decimals=6,
                total_raw=5000 * 10**6,
                transfer_count=1,
                asset="0xusdc",
            )
        )
        html = to_flow_html(g)
        assert '"symbol": "USDC"' in html and '"symbol": "ETH"' in html
        assert '"decimals": 6' in html

    def test_an_empty_graph_renders(self):
        assert "<svg" in to_flow_html(Graph(seeds=[]))
