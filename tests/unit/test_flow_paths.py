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
