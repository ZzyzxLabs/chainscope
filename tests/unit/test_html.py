"""The self-contained fund-flow view.

Two properties carry the weight.

**It must fetch nothing.** An exhibit attached to a case bundle has to open on
a machine with no network, five years from now. One CDN reference makes it a
document that works until it does not, and it will stop working precisely when
somebody needs it.

**It must not let a reader mistake an incomplete graph for a complete one.**
The frontier and the truncation flag have to survive into the rendered page,
not just into the data behind it.
"""

import json
import re

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.render.graph import Edge, Graph, Node
from chainscope.render.html import to_html

CHAIN = str(ETHEREUM)
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40

TEN_ETH = 10 * 10**18


@pytest.fixture
def graph():
    g = Graph(seeds=[f"{CHAIN}:{A}"])
    g.add_node(Node(address=A, chain=CHAIN, is_seed=True, expanded=True))
    g.add_node(Node(address=B, chain=CHAIN, expanded=True))
    g.add_node(Node(address=C, chain=CHAIN))  # frontier
    g.add_edge(Edge(A, B, CHAIN, "ETH", TEN_ETH, transfer_count=3))
    g.add_edge(Edge(B, C, CHAIN, "USDC", 5_000_000, decimals=6, transfer_count=1))
    g.attribute(
        B,
        CHAIN,
        Attribution(
            label="Tornado Cash",
            category=Category.SANCTIONED,
            confidence=Confidence.CERTAIN,
            method=Method.LIST,
            source="OFAC SDN",
            address=B,
            chain=ETHEREUM,
        ),
    )
    return g


class TestSelfContainment:
    def test_nothing_is_fetched_at_load_time(self, graph):
        page = to_html(graph)
        assert not re.search(r"<script[^>]+\bsrc\s*=", page, re.I)
        assert not re.search(r"<link[^>]+href\s*=\s*[\"']https?://", page, re.I)
        assert not re.search(r"@import\s+url\(", page, re.I)

    def test_no_remote_hosts_appear_at_all(self, graph):
        """Including in the JS: a fetch() would be just as fatal to an offline
        exhibit as a script tag."""
        page = to_html(graph)
        for marker in ("cdn.", "unpkg", "jsdelivr", "googleapis", "fetch(", "XMLHttpRequest"):
            assert marker not in page

    def test_it_is_one_document(self, graph):
        page = to_html(graph)
        assert page.lstrip().startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")

    def test_it_renders_without_a_graph(self):
        """An empty case should still open rather than throwing in the page."""
        page = to_html(Graph())
        assert "<svg" in page
        assert "none" in page


class TestIncompletenessIsVisible:
    def test_the_frontier_count_is_in_the_header(self, graph):
        assert "1 frontier" in to_html(graph)

    def test_frontier_nodes_are_flagged_in_the_data(self, graph):
        page = to_html(graph)
        payload = json.loads(re.search(r"const DATA = (\{.*?\});", page, re.S).group(1))
        frontier = [n for n in payload["nodes"] if n["frontier"]]
        assert [n["address"] for n in frontier] == [C]

    def test_the_page_explains_what_a_dashed_node_means(self, graph):
        """A visual convention nobody documented is a visual convention nobody
        reads correctly."""
        page = to_html(graph)
        assert "seen but never" in page
        assert "not because" in page

    def test_truncation_is_announced_loudly(self):
        g = Graph(truncated=True)
        g.add_node(Node(A, CHAIN))
        page = to_html(g)
        assert "TRUNCATED" in page
        assert "not the whole case" in page

    def test_a_complete_graph_says_nothing_about_truncation(self, graph):
        assert "TRUNCATED" not in to_html(graph)


class TestNumbers:
    def test_amounts_cross_as_strings(self, graph):
        """A JSON number is a double in the browser, and 10 ETH exceeds what
        one holds exactly."""
        page = to_html(graph)
        payload = json.loads(re.search(r"const DATA = (\{.*?\});", page, re.S).group(1))
        raw = payload["links"][0]["total_raw"]
        assert isinstance(raw, str)
        assert int(raw) == TEN_ETH

    def test_formatting_happens_on_digits_not_floats(self, graph):
        """The page formats by string manipulation; a parseFloat anywhere in
        the amount path would silently round."""
        page = to_html(graph)
        assert "function human(raw, decimals)" in page
        assert "parseFloat" not in page
        assert "Number(raw" not in page

    def test_totals_are_listed_per_asset(self, graph):
        """Summing across assets produces a figure denominated in nothing."""
        page = to_html(graph)
        assert str(TEN_ETH) in page
        assert "5000000" in page
        assert "never combined across assets" in page


class TestAttribution:
    def test_a_label_reaches_the_page(self, graph):
        assert "Tornado Cash" in to_html(graph)

    def test_confidence_travels_with_it(self, graph):
        page = to_html(graph)
        payload = json.loads(re.search(r"const DATA = (\{.*?\});", page, re.S).group(1))
        labelled = next(n for n in payload["nodes"] if n["label"] == "Tornado Cash")
        assert labelled["confidence"] == int(Confidence.CERTAIN)
        assert labelled["source"] == "OFAC SDN"

    def test_unlabelled_addresses_stay_visibly_unlabelled(self, graph):
        page = to_html(graph)
        assert "2 unlabelled" in page

    def test_a_hostile_label_cannot_inject_markup(self):
        """A label can come from an imported file or an agent. It is untrusted
        text that lands in both an HTML attribute and a JSON literal."""
        g = Graph()
        g.add_node(Node(A, CHAIN, label="</script><img src=x onerror=alert(1)>", confidence=3))
        page = to_html(g, title="</title><script>alert(2)</script>")
        assert "<img src=x" not in page
        assert "<script>alert(2)</script>" not in page

    def test_the_title_is_escaped(self):
        page = to_html(Graph(), title="Case <b>7</b> & co")
        assert "&lt;b&gt;" in page
        assert "&amp;" in page
