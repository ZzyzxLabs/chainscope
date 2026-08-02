"""The page the tool actually writes must be valid JavaScript.

This is here because it was not, and had not been for the whole life of the
view. A stray `\\"` inside the Python template collapsed to a bare quote in the
emitted JS, so the entire `<script>` failed to parse and **nothing ran** --- no
graph, no click-to-expand, no time scrub. The file opened, rendered a header,
and showed an empty canvas.

Every JavaScript test in this suite passed throughout. They extract individual
functions with a regex and run them under Node, which is the right way to test
the route finder and the reveal rule --- and it is exactly why none of them
could see this. A fragment that parses tells you nothing about the file.

So this one is deliberately dumb: render a real graph, take the script tag the
user's browser would take, and ask Node whether it is a program.
"""

from __future__ import annotations

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


def page() -> str:
    """A graph with everything the template branches on switched on."""
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
    store.close()
    return to_flow_html(
        graph,
        title='case "one" <b>',
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
    )


def script_of(html: str) -> str:
    body = html.split("<script>", 1)[1]
    return body.split("</script>", 1)[0]


class TestTheEmittedPage:
    def test_the_script_is_a_valid_program(self, tmp_path: Path) -> None:
        js = tmp_path / "page.js"
        js.write_text(script_of(page()), encoding="utf-8")
        result = subprocess.run(
            ["node", "--check", str(js)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, (
            "the emitted page is not valid JavaScript, so none of it runs:\n" + result.stderr
        )

    def test_it_has_exactly_one_script_block(self) -> None:
        # Two would mean a template edit split it; the check above would then
        # only cover half the program.
        html = page()
        assert html.count("<script>") == 1
        assert html.count("</script>") == 1

    def test_data_cannot_close_the_script_tag(self) -> None:
        """A label containing `</script>` must not end the block early.

        That is the same class of defect as the one this file was written for
        --- content deciding where the program ends --- and it arrives from the
        store rather than from the template.
        """
        html = page()
        head, _, tail = html.partition("</script>")
        assert "function draw" in head, "the script was cut short by embedded data"
        assert "</body>" in tail
