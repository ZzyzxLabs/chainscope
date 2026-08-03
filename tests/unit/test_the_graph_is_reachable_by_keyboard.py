"""Selecting an address must not require a mouse.

Measured on the running page: 27 nodes, none in the tab order. Selecting an
address is the primary interaction of this tool, and it was reachable only by
pointing at it. The roster list gave a keyboard path to the same action, but a
graph nobody can enter is not an accessible graph --- it is a picture with a
workaround beside it.

Focus was also invisible: the CSS reset removed the browser's default ring and
nothing replaced it, so a keyboard user could move through 79 controls without
ever seeing where they were.
"""

from __future__ import annotations

from pathlib import Path

import pytest

GRAPH = Path("web/src/components/graph.tsx")
CSS = Path("web/src/app/globals.css")


@pytest.fixture(scope="module")
def graph() -> str:
    return GRAPH.read_text()


def test_nodes_are_in_the_tab_order(graph: str) -> None:
    assert "tabIndex={0}" in graph


def test_nodes_announce_themselves(graph: str) -> None:
    """A screen reader saying "button" 27 times is not a graph."""
    assert 'role="button"' in graph
    assert "aria-label=" in graph


def test_a_frontier_node_says_so_in_its_label(graph: str) -> None:
    """The dashed border is invisible to a reader who cannot see it, and it
    carries the difference between "the money stopped" and "nobody looked"."""
    # The node's label, not the svg's --- take the one beside role="button".
    block = graph.split('role="button"')[1][:500]
    assert "aria-label=" in block
    assert "frontier" in block, "a dashed border is invisible to a screen reader"


def test_enter_and_space_select(graph: str) -> None:
    """Both, because a role="button" is expected to answer both."""
    block = graph.split("onKeyDown=")[1][:220]
    assert '"Enter"' in block and '" "' in block
    assert "preventDefault" in block, "Space would scroll the page as well"


def test_focus_is_visible() -> None:
    css = CSS.read_text()
    assert ":focus-visible" in css
    assert "outline:" in css.split(":focus-visible")[1][:160]


def test_focused_nodes_are_visible_against_their_own_border() -> None:
    """A card already has a border, so the ring has to differ from it."""
    css = CSS.read_text()
    block = css.split(".card:focus-visible rect")[1][:200]
    assert "stroke-width" in block
