"""Every page the tool writes must be a program a browser can run.

This is here because it was not, and had not been for the whole life of the
view. A stray `\\"` inside the Python template collapsed to a bare quote in the
emitted JS, so the entire `<script>` failed to parse and **nothing ran** --- no
graph, no click-to-expand, no time scrub. The file opened, rendered a header,
and showed an empty canvas.

Every JavaScript test in this suite passed throughout. They extract individual
functions with a regex and run them under Node, which is the right way to test
the route finder and the reveal rule --- and it is exactly why none of them
could see this. A fragment that parses tells you nothing about the file.

So this one is deliberately dumb: render each page for real, take the script
tags a browser would take, and ask Node whether they are programs.

It covers the family rather than the one file that was broken. `flow` was the
only casualty --- the d3 view parses, the extension parses, and the dashboard
embeds `type="application/json"`, which is a data island and is checked as JSON
instead. But the defect was a template escape, and every one of these is built
by pasting strings into a template.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chainscope.cli.commands.graph import _walk
from chainscope.core.chainid import ETHEREUM
from chainscope.render.flow import to_flow_html

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def _store() -> tuple[object, object]:
    """A small case with everything the templates branch on switched on."""
    from datetime import datetime, timezone

    from chainscope.core.attribution import Attribution, Category, Confidence, Method
    from chainscope.core.models import Address, Transfer, TransferKind, TxRef
    from chainscope.core.units import Amount
    from chainscope.store.sqlite import SqliteStore

    store = SqliteStore(":memory:")
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, src, src),
                recipient=Address(ETHEREUM, dst, dst),
                amount=Amount(10**19, 18, "ETH"),
                kind=TransferKind.NATIVE,
                timestamp=datetime(2026, 6, 1 + i, tzinfo=timezone.utc),
                block=20_000_000 + i,
            )
            for i, (src, dst) in enumerate([(A, B), (B, C), (A, C)])
        ]
    )
    store.put_attributions(
        [
            Attribution(
                address=B,
                chain=ETHEREUM,
                label='a label with "quotes" and <tags>',
                category=Category.MIXER,
                confidence=Confidence.MEDIUM,
                method=Method.HEURISTIC,
                source="test",
                rationale="why",
            )
        ]
    )
    for address in (A, B):
        store.mark_expanded(address, ETHEREUM)
    graph = _walk(store, A, ETHEREUM, depth=2, max_nodes=50, per_node=10, direction="out")
    return store, graph


#: Every page the tool writes that a browser executes or parses. The label and
#: the note carry a quote, an angle bracket and a literal `</script>`, so each
#: page is checked against data trying to end the block as well as against the
#: template's own escaping.
def pages() -> dict[str, str]:
    from chainscope.cli.commands.dashboard import build_summary
    from chainscope.render.dashboard import to_dashboard
    from chainscope.render.html import to_html

    store, graph = _store()
    title = 'case "one" <b> </script>'
    out = {
        "flow": to_flow_html(
            graph,
            title=title,
            visible_depth=1,
            notes={
                B: [
                    {
                        "kind": "observation",
                        "body": 'note with "quotes" and </script>',
                        "by": "alice@lab",
                        "at": "2026-08-01",
                        "superseded": False,
                    }
                ]
            },
        ),
        "graph": to_html(graph, title=title),
    }
    import tempfile
    from pathlib import Path as _Path

    # The dashboard reads a store from disk rather than an object.
    disk = _Path(tempfile.mkdtemp()) / "store.db"
    from chainscope.store.sqlite import SqliteStore as _S

    copy = _S(disk)
    copy.put_transfers(list(store.transfers(_query())))
    copy.put_attributions([c for a in (A, B, C) for c in store.attributions(a)])
    copy.close()
    store.close()
    out["dashboard"] = to_dashboard(build_summary(disk, title=title))
    return out


def _query():
    from chainscope.store.base import Query

    return Query(limit=1000)


def blocks(html: str) -> list[tuple[str, str]]:
    """``(type attribute, body)`` for every script element on the page."""
    return [
        (m.group(1) or "", m.group(2))
        for m in re.finditer(r'<script(?:\s+type="([^"]*)")?[^>]*>(.*?)</script>', html, re.S)
    ]


ALL = pages()


@pytest.mark.parametrize("name", sorted(ALL))
class TestEveryEmittedPage:
    def test_its_scripts_are_valid_programs(self, name: str, tmp_path: Path) -> None:
        for i, (kind, body) in enumerate(blocks(ALL[name])):
            if kind and kind != "text/javascript":
                continue  # a data island; checked below
            js = tmp_path / f"{name}-{i}.js"
            js.write_text(body, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(js)], capture_output=True, text=True, check=False
            )
            assert result.returncode == 0, (
                f"{name}: script block {i} is not valid JavaScript, so none of "
                f"it runs:\n{result.stderr}"
            )

    def test_its_data_islands_are_valid_json(self, name: str) -> None:
        import json

        for kind, body in blocks(ALL[name]):
            if kind == "application/json":
                json.loads(body)

    def test_it_has_at_least_one_script(self, name: str) -> None:
        # A page with none means the regex stopped matching --- at which point
        # every assertion above passes vacuously, which is how a check like
        # this quietly stops checking.
        assert blocks(ALL[name]), f"{name} has no script element to check"

    def test_embedded_data_cannot_end_a_script_block(self, name: str) -> None:
        """A label containing `</script>` must not close the element early.

        Same class of defect as the one this file was written for --- content
        deciding where the program ends --- but arriving from the store rather
        than from the template.
        """
        assert "</script>" not in "".join(b for _, b in blocks(ALL[name]))
