"""The rewritten front end must not quietly lose a control.

`export` existed on the inline page and did not survive the port to Next. It
was found by listing both pages' buttons and comparing, not by anyone missing
it --- which is the problem with a rewrite: the absence looks like a design
choice.

Verified against the running page: the restored export produced an 18KB SVG
with 27 nodes, styles inlined and the xmlns set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INLINE = Path("src/chainscope/server/webapp.py")
NEXT = Path("web/src/app/case/page.tsx")
PANEL = Path("web/src/components/selected.tsx")

#: Controls the inline page offers. A rewrite may deliberately drop one --- it
#: must then be removed from here, which makes the decision explicit.
EXPECTED = ["ask", "share", "export", "undo", "redo"]


@pytest.fixture(scope="module")
def next_ui() -> str:
    return NEXT.read_text() + PANEL.read_text()


@pytest.mark.parametrize("control", EXPECTED)
def test_the_control_survived(control: str, next_ui: str) -> None:
    assert re.search(rf">\s*{control}\s*<", next_ui), f"{control} was lost in the port"


def test_the_inline_page_offers_nothing_the_port_does_not() -> None:
    """Catches the next one, whatever it turns out to be."""
    inline = INLINE.read_text()
    ids = set(re.findall(r'<button id="(\w+)"', inline))
    # Dialog and widget internals. The port reaches the same actions through
    # React state rather than an element id, so an id-by-id comparison cannot
    # see them. Only whole *capabilities* belong in EXPECTED above -- that is
    # the list which catches a lost feature.
    excused = {
        "addbtn",  # "+ add" -- the panel expands from a selected node instead
        "addgo",
        "addclose",
        "askbtn",  # ask dialog, componentised
        "askgo",
        "askrun",
        "askclose",
        "xgo",  # "expand one hop", now lives in the panel
        "notesave",
        "zfit",
        "zin",
        "zout",  # note form and zoom controls
    }
    missing = {i for i in ids - excused if i not in NEXT.read_text() + PANEL.read_text()}
    assert not missing, f"inline page has controls the port lacks: {sorted(missing)}"


def test_export_inlines_its_styles(next_ui: str) -> None:
    """A picture that fetches a stylesheet to render is one that stops
    rendering, and that tells somebody it was opened."""
    block = next_ui.split("function exportSvg")[1][:2200]
    assert "cssRules" in block and "createElementNS" in block


def test_export_writes_the_whole_graph_not_the_viewport(next_ui: str) -> None:
    """What belongs in a report is the case, not the corner of it on screen."""
    block = next_ui.split("function exportSvg")[1][:2200]
    assert "data-width" in block and "viewBox" in block
