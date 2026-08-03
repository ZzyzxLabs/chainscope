"""The highlighted path on the graph must be one the money could have taken.

`pathsTo`, the flow page's route search, followed any outgoing edge. So it lit
up paths where a hop happened *before* the money arrived --- which money cannot
do. Measured on a real ledger of 55 transfers between 37 addresses: 62% of the
multi-hop paths a time-blind search returns are causally impossible.

On a picture this matters more than in a list. A highlighted line on a graph
reads as a fact, and nobody goes behind it to check timestamps. It is the layer
where being wrong is least likely to be caught.

`chainscope.analysis.route` already did this correctly over unaggregated
transfers. This file holds the browser's copy to the same rule, by running it in
node --- not by reading it. A test that greps the source for a comparison would
pass on a comparison that never runs.

One difference is deliberate and is checked here. An edge on the page is an
*aggregate* over a span, not a moment, so the test is `e.last >= since` rather
than `e.first >= since`: an edge whose span began before the money arrived but
which also carried transfers afterwards is usable, and dropping it would lose
real routes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import ClassVar

import pytest

from chainscope.render import flow

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _script() -> str:
    """The page's script block. `_PAGE` holds the whole document."""
    page = flow._PAGE
    return page[page.index("<script>") : page.rindex("</script>")]


def _run(nodes: list[dict], edges: list[dict], seeds: list[str], target: str) -> dict:
    """Execute the page's own `pathsTo` against a small graph."""
    # The cap is read from the page, not restated here. Hardcoding it meant the
    # test would keep exercising 40 after somebody changed the page to 5, and
    # would pass while testing a bound that no longer exists.
    body = re.search(r"const MAX_PATHS = (\d+);\n(function pathsTo.*?\n\})", _script(), re.S)
    assert body, "pathsTo not found --- has it been renamed?"
    program = (
        f"const DATA = {json.dumps({'nodes': nodes, 'edges': edges, 'seeds': seeds})};\n"
        f"const MAX_PATHS = {body.group(1)};\n"
        + body.group(2)
        + f"\nconst r = pathsTo({json.dumps(target)}, DATA.edges);\n"
        "console.log(JSON.stringify({"
        "hops: r.hops.map(h => h.map(e => e.source + '>' + e.target)),"
        " impossible: r.impossible, capped: r.capped}));\n"
    )
    # A timeout, because one of the tests below is called
    # `test_a_cycle_does_not_hang_it` and without this it would hang forever
    # proving the opposite --- pytest would sit there rather than fail.
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True, timeout=30
    )
    return dict(json.loads(out.stdout))


def _edge(source: str, target: str, first: int, last: int | None = None) -> dict:
    return {
        "source": source,
        "target": target,
        "asset": "",
        "first": first,
        "last": last if last is not None else first,
    }


class TestTheImpossiblePathIsNotHighlighted:
    #: x pays b at t=5, before a pays x at t=10. y pays b at t=20. Only y is real.
    EDGES: ClassVar[list[dict]] = [
        _edge("a", "x", 10),
        _edge("x", "b", 5),
        _edge("a", "y", 10),
        _edge("y", "b", 20),
    ]

    def test_only_the_possible_route_comes_back(self) -> None:
        result = _run([], self.EDGES, ["a"], "b")
        assert result["hops"] == [["a>y", "y>b"]]

    def test_the_impossible_one_is_counted(self) -> None:
        # Counted, not silently dropped: "there is one route here" reads very
        # differently from "there is one, and another the timestamps rule out".
        result = _run([], self.EDGES, ["a"], "b")
        assert result["impossible"] >= 1

    def test_a_direct_edge_from_the_seed_is_always_allowed(self) -> None:
        # There is no prior arrival to be after.
        result = _run([], [_edge("a", "b", 1)], ["a"], "b")
        assert result["hops"] == [["a>b"]]


class TestAnEdgeIsASpanNotAMoment:
    def test_an_edge_that_began_early_but_continued_is_usable(self) -> None:
        """The distinction that makes this different from the Python version.

        The second edge's span starts at t=1, before the money arrives at t=10,
        but it runs to t=30 --- so some transfer within it can have happened
        afterwards. Testing `first` would have thrown this route away.
        """
        edges = [_edge("a", "m", 10), _edge("m", "b", 1, 30)]
        assert _run([], edges, ["a"], "b")["hops"] == [["a>m", "m>b"]]

    def test_an_edge_that_had_finished_is_not(self) -> None:
        edges = [_edge("a", "m", 10), _edge("m", "b", 1, 5)]
        assert _run([], edges, ["a"], "b")["hops"] == []

    def test_arrival_carries_forward_as_the_later_of_the_two(self) -> None:
        # Money arrives at m at t=10; the m->n edge spans 1..30, so the earliest
        # onward moment is 10, not 1 --- and n->b at t=6 is therefore refused.
        edges = [_edge("a", "m", 10), _edge("m", "n", 1, 30), _edge("n", "b", 6)]
        assert _run([], edges, ["a"], "b")["hops"] == []

    def test_and_a_later_onward_hop_is_allowed(self) -> None:
        edges = [_edge("a", "m", 10), _edge("m", "n", 1, 30), _edge("n", "b", 40)]
        assert _run([], edges, ["a"], "b")["hops"] == [["a>m", "m>n", "n>b"]]


class TestItStillFindsWhatItShould:
    def test_a_split_that_rejoins_gives_two_routes(self) -> None:
        # The structure the search returns every path for, rather than the
        # shortest: somebody split the funds for a reason.
        edges = [
            _edge("a", "m", 1),
            _edge("m", "b", 5),
            _edge("a", "n", 1),
            _edge("n", "b", 5),
        ]
        assert len(_run([], edges, ["a"], "b")["hops"]) == 2

    def test_a_cycle_does_not_hang_it(self) -> None:
        edges = [_edge("a", "m", 1), _edge("m", "n", 2), _edge("n", "m", 3), _edge("m", "b", 4)]
        result = _run([], edges, ["a"], "b")
        assert result["hops"] == [["a>m", "m>b"]]

    def test_simultaneous_hops_are_allowed(self) -> None:
        # Two transfers in one block share a timestamp; refusing that would drop
        # real routes.
        assert _run([], [_edge("a", "m", 7), _edge("m", "b", 7)], ["a"], "b")["hops"]


class TestItAgreesWithThePythonImplementation:
    """Same ledger, both searches, same verdict.

    The page and `chainscope analyze route` answer the same question and sit
    next to each other in a report. Disagreeing would be worse than either
    being wrong alone.
    """

    LEDGER: ClassVar[list[tuple[str, str, int]]] = [
        ("a", "x", 10),
        ("x", "b", 5),
        ("a", "y", 10),
        ("y", "b", 20),
    ]

    def test_both_reject_the_impossible_route(self) -> None:
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace

        from chainscope.analysis.route import find_routes

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            SimpleNamespace(
                sender=SimpleNamespace(key=s),
                recipient=SimpleNamespace(key=r),
                timestamp=base + timedelta(minutes=t),
                asset=None,
                amount=SimpleNamespace(raw=1, symbol=""),
                tx=SimpleNamespace(hash=""),
            )
            for s, r, t in self.LEDGER
        ]
        python_routes, _ = find_routes(rows, "a", "b")
        browser = _run([], [_edge(s, r, t) for s, r, t in self.LEDGER], ["a"], "b")

        assert [r.addresses for r in python_routes] == [["a", "y", "b"]]
        assert browser["hops"] == [["a>y", "y>b"]]
