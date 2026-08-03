"""One wei must not render as zero, in either implementation.

Both formatters cut the fraction at a fixed position --- six digits server-side,
four in the browser --- after stripping trailing zeros. So one wei became
``0.000000`` and 0.000012345 ETH became ``0.000012``. The first reads as *nothing
moved*, which is the wrong thing to tell somebody looking at a peel chain or an
address-poisoning transfer: there, the dust amount is the entire signal.

There are two implementations because one runs in Python and one in the emitted
page's JavaScript, and a graph rendered by the dashboard sits next to one
rendered by the flow page. They have to agree on what a number looks like, so
this file runs both over the same inputs.

It also parses the whole of `html.py`'s script rather than the one function.
`render/flow.py` shipped JavaScript that had never parsed, and every test passed
throughout because they each extracted one function with a regex --- a fragment
that parses says nothing about the file it came from.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from decimal import Decimal

import pytest

from chainscope.render.dashboard import _fmt

#: ``(raw, decimals)``. The first three are the defect; the rest are ordinary
#: amounts that must not have changed.
CASES = [
    ("1", 18),
    ("12345000000000", 18),
    ("999", 18),
    ("1000000000000000000", 18),
    ("1500000000000000000", 18),
    ("20000000000000000000", 18),
    ("1000000000", 6),
    ("1", 6),
    ("0", 18),
    ("-1500000000000000000", 18),
    ("123456789012345678901234567890", 18),
]


class TestSmallAmountsSurvive:
    def test_one_wei_is_not_zero(self) -> None:
        rendered = _fmt("1")
        assert rendered != "0.000000"
        assert Decimal(rendered.replace(",", "")) > 0

    def test_the_digits_are_the_real_digits(self) -> None:
        assert _fmt("12345000000000") == "0.000012345"

    def test_an_ordinary_amount_is_unchanged(self) -> None:
        assert _fmt("1500000000000000000") == "1.5"
        assert _fmt("1000000000", 6) == "1,000"

    def test_zero_is_still_zero(self) -> None:
        # The one case where "0" is the honest answer.
        assert _fmt("0") == "0"

    def test_a_large_amount_is_not_padded_with_significant_noise(self) -> None:
        # The extra digits are only kept when the whole part is zero. A number
        # with an integer part does not need twenty decimals to be legible.
        assert _fmt("123456789012345678901234567890") == "123,456,789,012.345678"

    def test_negatives_keep_their_sign(self) -> None:
        assert _fmt("-1500000000000000000") == "-1.5"


def _script() -> str:
    from chainscope.render import html

    return str(html._JS)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestTheBrowserAgrees:
    def test_the_whole_script_parses(self) -> None:
        """Not one function --- the file.

        `render/flow.py`'s script had never parsed and every test passed, because
        each one pulled a single function out with a regex first.
        """
        source = _script()
        for placeholder in ("__DATA__", "__PALETTE__", "__TITLE__", "__NOTE__"):
            source = source.replace(placeholder, "null")
        result = subprocess.run(
            ["node", "--check", "-"], input=source, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_both_implementations_render_the_same_amounts(self) -> None:
        """Four significant digits in the browser, six here --- so the browser's
        output must be a prefix of this one, not a different number."""
        body = re.search(r"function human\(raw, decimals\) \{.*?\n\}", _script(), re.S)
        assert body, "human() not found"
        program = (
            body.group(0)
            + "\nconst out = "
            + json.dumps(CASES)
            + ".map(([r, d]) => human(r, d));\n"
            + "console.log(JSON.stringify(out));\n"
        )
        result = subprocess.run(
            ["node", "-e", program], capture_output=True, text=True, check=True
        )
        for (raw, decimals), browser in zip(CASES, json.loads(result.stdout), strict=True):
            server = _fmt(raw, decimals)
            assert server.startswith(browser), (
                f"{raw} at {decimals} decimals: browser {browser!r}, server {server!r}"
            )

    def test_the_browser_does_not_render_one_wei_as_zero_either(self) -> None:
        body = re.search(r"function human\(raw, decimals\) \{.*?\n\}", _script(), re.S)
        assert body
        result = subprocess.run(
            ["node", "-e", body.group(0) + '\nconsole.log(human("1", 18));'],
            capture_output=True,
            text=True,
            check=True,
        )
        assert Decimal(result.stdout.strip()) > 0
