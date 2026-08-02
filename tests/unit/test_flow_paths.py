"""The route finder, executed rather than eyeballed.

Path highlighting is the substantive half of the flow view and it lives in
JavaScript, where the Python tests cannot reach it. Shipping it unexercised
would repeat the mistake this project keeps finding: something that looks right
and has never been run.

So the function is extracted from the generated page and executed under Node.
The test skips where Node is absent rather than passing vacuously --- a skip is
visible and a silent pass is not.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
from typing import ClassVar

import pytest

from chainscope.render.flow import to_flow_html
from chainscope.render.graph import Graph

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

A, B, C, D, E = ("0x" + c * 40 for c in "abcde")


def _script() -> str:
    """`pathsTo` and its constant, lifted out of the page it ships in."""
    page = to_flow_html(Graph(seeds=[]))
    body = page.split("<script>", 1)[1].split("</script>", 1)[0]
    match = re.search(r"const MAX_PATHS = \d+;\nfunction pathsTo.*?\n}\n", body, re.DOTALL)
    assert match, "pathsTo not found in the generated page"
    return match.group(0)


def run(seeds, edges, target):
    js = f"""
const DATA = {{seeds: {json.dumps(seeds)}}};
{_script()}
const r = pathsTo({json.dumps(target)}, {json.dumps(edges)});
console.log(JSON.stringify({{
  nodes: [...r.nodes].sort(),
  edges: [...r.edges].sort(),
  hops: r.hops.length,
  capped: !!r.capped,
}}));
"""
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def edge(source, target, asset=""):
    return {"source": source, "target": target, "asset": asset}


class TestItFindsRoutes:
    def test_a_straight_chain(self):
        r = run([A], [edge(A, B), edge(B, C), edge(C, D)], D)
        assert r["hops"] == 1
        assert r["nodes"] == sorted([A, B, C, D])

    def test_only_the_route_to_the_target_lights_up(self):
        """A sibling branch is not part of this route and must stay dark."""
        r = run([A], [edge(A, B), edge(B, C), edge(A, E)], C)
        assert E not in r["nodes"]

    def test_a_split_that_rejoins_gives_two_routes(self):
        """The structure worth seeing. A shortest-path search would show one
        and hide the fact that the funds were split deliberately."""
        r = run([A], [edge(A, B), edge(A, C), edge(B, D), edge(C, D)], D)
        assert r["hops"] == 2
        assert set(r["nodes"]) == {A, B, C, D}

    def test_two_seeds_both_count(self):
        r = run([A, B], [edge(A, C), edge(B, C)], C)
        assert r["hops"] == 2

    def test_an_unreachable_target_has_no_route(self):
        r = run([A], [edge(A, B), edge(C, D)], D)
        assert r["hops"] == 0
        assert r["nodes"] == []


class TestItTerminates:
    def test_a_cycle_does_not_hang(self):
        """A cycle is not a route. Without the visited set this recurses
        forever and the page freezes."""
        r = run([A], [edge(A, B), edge(B, C), edge(C, A), edge(C, D)], D)
        assert r["hops"] == 1

    def test_a_self_loop_does_not_hang(self):
        r = run([A], [edge(A, A), edge(A, B)], B)
        assert r["hops"] == 1

    def test_a_dense_graph_caps_and_says_so(self):
        """Bounded rather than slow. Hitting the cap is reported, because a
        truncated set of routes presented as all of them is the same failure
        this whole project is arranged against."""
        layers = [[f"0x{i}{j}" for j in range(4)] for i in range(6)]
        edges = [edge(A, n) for n in layers[0]]
        for lo, hi in itertools.pairwise(layers):
            edges += [edge(x, y) for x in lo for y in hi]
        edges += [edge(n, D) for n in layers[-1]]
        r = run([A], edges, D)
        assert r["capped"] is True
        assert r["hops"] <= 40


class TestAssetsStaySeparate:
    def test_the_same_pair_in_two_assets_is_two_edges(self):
        """Edge identity includes the asset, so highlighting an ETH route does
        not light a USDC one between the same addresses."""
        r = run([A], [edge(A, B, "eth"), edge(A, B, "0xusdc")], B)
        assert len(r["edges"]) == 2
        assert any("0xusdc" in k for k in r["edges"])


def _reveal_script() -> str:
    """`isShown` and `hiddenBehind`, lifted out of the page."""
    page = to_flow_html(Graph(seeds=[]))
    body = page.split("<script>", 1)[1].split("</script>", 1)[0]
    m = re.search(r"function isShown.*?\n}\n\nfunction hiddenBehind.*?\n}\n", body, re.DOTALL)
    assert m, "reveal helpers not found in the generated page"
    return m.group(0)


def reveal(nodes, edges, opened):
    js = f"""
const DATA = {{nodes: {json.dumps(nodes)}, edges: {json.dumps(edges)}}};
const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const opened = new Set({json.dumps(opened)});
{_reveal_script()}
console.log(JSON.stringify({{
  shown: DATA.nodes.filter(isShown).map(n => n.id).sort(),
  folded: Object.fromEntries(DATA.nodes.map(n => [n.id, hiddenBehind(n.id)])),
}}));
"""
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def node(nid, collapsed=False):
    return {"id": nid, "collapsed": collapsed}


class TestClickToExpand:
    """A file:// page cannot fetch, so the extra hop ships folded. Opening it
    reveals what was walked and no more --- the outermost ring stays frontier
    however often it is clicked, because a control that quietly stopped
    producing nodes would read as 'the money ends here'."""

    NODES: ClassVar = [node(A), node(B), node(C, collapsed=True), node(D, collapsed=True)]
    EDGES: ClassVar = [edge(A, B), edge(B, C), edge(C, D)]

    def test_collapsed_nodes_start_hidden(self):
        r = reveal(self.NODES, self.EDGES, [])
        assert r["shown"] == sorted([A, B])

    def test_the_parent_advertises_how_many_are_folded(self):
        r = reveal(self.NODES, self.EDGES, [])
        assert r["folded"][B] == 1

    def test_opening_a_parent_reveals_its_children(self):
        r = reveal(self.NODES, self.EDGES, [B])
        assert C in r["shown"]

    def test_but_only_one_ring_at_a_time(self):
        """Opening B shows C. D stays folded behind C, so the reader always
        knows there is more rather than being handed a picture that stops."""
        r = reveal(self.NODES, self.EDGES, [B])
        assert D not in r["shown"]
        assert r["folded"][C] == 1

    def test_an_opened_node_no_longer_advertises_a_badge(self):
        r = reveal(self.NODES, self.EDGES, [B])
        assert r["folded"][B] == 0

    def test_a_node_with_nothing_folded_has_no_badge(self):
        r = reveal([node(A), node(B)], [edge(A, B)], [])
        assert r["folded"][A] == 0

    def test_two_parents_of_one_collapsed_node(self):
        """Opening either reveals it; the fold is per-edge, not per-node."""
        nodes = [node(A), node(B), node(C), node(D, collapsed=True)]
        edges = [edge(A, B), edge(A, C), edge(B, D), edge(C, D)]
        assert D in reveal(nodes, edges, [B])["shown"]
        assert D in reveal(nodes, edges, [C])["shown"]
