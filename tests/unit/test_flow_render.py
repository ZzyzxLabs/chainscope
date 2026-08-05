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


class TestTheEdgePanel:
    """MetaSleuth has an Edge Panel: click a flow, see what it is made of. The
    most common question while reading a graph, and this had no answer --- edges
    were aggregates with nothing behind them."""

    def test_edges_have_a_click_target(self):
        """A hairline is unclickable, and "I cannot hit the thing I want to
        inspect" is the most common way a graph view gets abandoned."""
        html = to_flow_html(chain_graph())
        assert 'stroke", "transparent"' in html
        assert 'stroke-width", "12"' in html

    def test_the_panel_says_an_aggregate_is_not_one_payment(self):
        """A reader taking it for a single transfer is wrong about size and
        timing at once."""
        assert "not one payment" in to_flow_html(chain_graph())

    def test_it_admits_the_transfers_are_not_in_the_file(self):
        """A real case has more transfers than a page can hold, and claiming
        otherwise would be the same overreach as everything else here."""
        html = to_flow_html(chain_graph())
        assert "not in this file" in html

    def test_it_ends_in_something_runnable(self):
        """A dead end would be a worse answer than the aggregate."""
        html = to_flow_html(chain_graph())
        assert "chainscope sql" in html
        assert "FROM transfers WHERE" in html

    def test_the_query_distinguishes_native_from_a_token(self):
        """`asset IS NULL` and `asset = '0x...'` are different rows, and one
        query for both would return the wrong transfers."""
        html = to_flow_html(chain_graph())
        assert "asset IS NULL" in html
        assert "asset = '" in html


class TestKeyboardAndDrag:
    """Two of the three remaining MetaSleuth gaps. Neither adds capability ---
    both remove the need to leave the picture to use one."""

    def test_the_shortcut_list_is_discoverable(self):
        """A shortcut nobody can find is a shortcut nobody has."""
        html = to_flow_html(chain_graph())
        assert "press ? for keys" in html

    def test_every_shortcut_is_documented_in_the_page(self):
        html = to_flow_html(chain_graph())
        for key in ("Esc", "cycle the asset", "expand every folded", "reset dragged"):
            assert key in html

    def test_dragging_moves_only_vertically(self):
        """x encodes hop distance from the seed. Letting a node slide between
        columns would let somebody rearrange the picture into a claim the data
        does not make."""
        html = to_flow_html(chain_graph())
        assert "never the column" in html
        assert "ev.clientY" in html
        assert "ev.clientX" not in html

    def test_dragged_positions_survive_a_redraw(self):
        """Switching asset or scrubbing time must not undo what the reader
        arranged."""
        html = to_flow_html(chain_graph())
        assert "keeps what the reader arranged" in html


class TestWhichSideOfTheSeedAFunderLandsOn:
    """A funder drawn to the right of the seed is a wrong answer, not a gap.

    `layer_nodes` walked forwards only, so an address that *paid* the seed was
    unreachable and fell through to the "put it in the last column" clause ---
    the rightmost one, which on a left-to-right flow diagram is where the money
    ended up. On the LpdFi case both addresses that staked the attacker, one of
    them with 689,529 USDC, were drawn downstream of him.

    An omitted funder is something a reader can notice is missing. A funder in
    the recipient's position is a picture that reads cleanly and says the
    opposite of what happened.
    """

    def _funded(self):
        """F → A → B. A is the seed; F paid it; B was paid by it."""
        funder = "0x" + "f" * 40
        g = chain_graph()
        g.add_node(Node(address=funder, chain=str(ETHEREUM), expanded=True))
        g.add_edge(
            Edge(
                source=funder,
                target=A,
                chain=str(ETHEREUM),
                symbol="ETH",
                decimals=18,
                total_raw=10**20,
                transfer_count=1,
            )
        )
        return g, funder

    def test_a_funder_sits_upstream_of_the_seed(self):
        g, funder = self._funded()
        depth = layer_nodes(g)
        assert depth[funder] < depth[A], (
            "the address that paid the seed has to be drawn on the side a "
            "reader takes to mean 'before'"
        )

    def test_a_funder_is_not_parked_in_the_last_column(self):
        """The specific old behaviour, pinned so it cannot come back."""
        g, funder = self._funded()
        depth = layer_nodes(g)
        assert depth[funder] < max(depth.values()), (
            "it used to get `furthest + 1`, which put it further downstream "
            "than every address the money actually reached"
        )

    def test_hop_distance_is_still_the_magnitude(self):
        g, funder = self._funded()
        depth = layer_nodes(g)
        assert depth[funder] == -1
        assert [depth[x] for x in (A, B, C, D)] == [0, 1, 2, 3]

    def test_an_address_on_both_sides_is_placed_upstream(self):
        """B is paid by the seed and also pays it.

        No column is true for such an address --- the arrows carry the
        direction, the column carries the emphasis --- and the emphasis goes to
        the half a reader cannot otherwise see. This is not hypothetical: both
        of the LpdFi attacker's funders were also paid by him, so with
        downstream winning they landed on the right again and the funding
        stayed invisible, which is the whole complaint this fixes.
        """
        g = chain_graph()
        g.add_edge(
            Edge(
                source=B,
                target=A,
                chain=str(ETHEREUM),
                symbol="ETH",
                decimals=18,
                total_raw=10**18,
                transfer_count=1,
            )
        )
        assert layer_nodes(g)[B] == -1
