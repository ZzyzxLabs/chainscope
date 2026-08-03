"""Every page this package emits must be JavaScript a browser can run.

`render/flow.py` once shipped a script that had never parsed --- a `\\"` in a
Python string became a bare quote, and the whole block was dead. Every test
passed throughout, because each one pulled a single function out with a regular
expression first, and a fragment that parses says nothing about the file it came
from. It was found by opening the page in a browser.

Two tests since then run `node --check` over a specific renderer's script. Both
would keep passing if somebody added a third renderer tomorrow, which is the
gap this closes: the modules are discovered from the package rather than listed
here, so a new one is covered on the day it appears.

It is also a test that must be able to fail. `test_a_broken_script_is_caught`
feeds `node --check` something invalid, because a harness whose checker silently
does nothing reports every page as fine.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import shutil
import subprocess

import pytest

import chainscope.render

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

#: Anything that looks like a substitution marker, replaced before parsing.
#: The template is not valid JavaScript until the data is in it, and what is
#: being checked is the code around the hole rather than the hole.
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


def _scripts() -> list[tuple[str, str]]:
    """Every script block in every renderer, found rather than listed."""
    found: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(chainscope.render.__path__):
        module = importlib.import_module(f"chainscope.render.{info.name}")
        for name in dir(module):
            if name.startswith("__"):
                continue
            value = getattr(module, name)
            if not isinstance(value, str) or len(value) < 200:
                continue
            for match in re.finditer(r"<script>(.*?)</script>", value, re.S):
                found.append((f"{info.name}.{name}", match.group(1)))
            # A module may hold its script separately from its page, as
            # `html.py` does. Recognised by name so an ordinary long string is
            # not fed to a JavaScript parser.
            if name == "_JS":
                found.append((f"{info.name}.{name}", value))
    return found


def _check(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--check", "-"],
        input=_PLACEHOLDER.sub("null", source),
        capture_output=True,
        text=True,
    )


def test_there_is_something_to_check() -> None:
    # Guards the discovery above. If the renderers were reorganised so nothing
    # matched, every test below would pass by finding nothing.
    assert len(_scripts()) >= 2


@pytest.mark.parametrize(
    "where,source", _scripts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else ""
)
def test_it_parses(where: str, source: str) -> None:
    result = _check(source)
    assert result.returncode == 0, f"{where} does not parse:\n{result.stderr}"


def test_a_broken_script_is_caught() -> None:
    """The check must be able to fail.

    A harness whose checker silently does nothing reports every page as fine,
    which is exactly the state this package was in while `flow.py`'s script was
    dead.
    """
    assert _check("function ({").returncode != 0


def test_placeholder_substitution_does_not_hide_a_syntax_error() -> None:
    # Replacing the markers must not be so aggressive that it repairs broken
    # code on its way past.
    assert _check("const x = __DATA__; function ({").returncode != 0
