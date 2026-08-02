"""Every MCP tool must appear in the list `doctor` prints.

`describe_tools()` named seven tools while the server registered ten. An agent
operator reading `chainscope doctor` would conclude three capabilities did not
exist --- the same failure as a stale skill, in a different file.

The list cannot be derived at runtime: `doctor` has to answer without the MCP
SDK installed, and building a server to ask it would make the answer unavailable
on precisely the install where somebody is checking what they would get. So it
is checked against the source instead, which is a contract test rather than a
list somebody remembers to update.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chainscope.agent.server import TOOLS

SOURCE = Path(__file__).resolve().parents[2] / "src" / "chainscope" / "agent" / "server.py"


def registered() -> set[str]:
    """Names of functions decorated with `@server.tool(...)`."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(call, ast.Attribute) and call.attr == "tool":
                found.add(node.name)
    return found


def listed() -> set[str]:
    # The list carries "(only with --writable)" for gated tools; that qualifier
    # is for a human reading `doctor` and is not part of the name.
    return {entry.split(" ")[0] for entry in TOOLS}


class TestTheListMatchesTheServer:
    @pytest.mark.parametrize("name", sorted(registered()))
    def test_every_registered_tool_is_listed(self, name: str) -> None:
        assert name in listed(), (
            f"the server registers `{name}` and `describe_tools()` does not name "
            f"it. An operator reading `chainscope doctor` will conclude it does "
            f"not exist."
        )

    @pytest.mark.parametrize("name", sorted(listed()))
    def test_every_listed_tool_is_registered(self, name: str) -> None:
        # The other direction matters as much: a tool that was removed and left
        # in the list promises a capability that is gone.
        assert name in registered(), (
            f"`{name}` is advertised and the server does not register it"
        )

    def test_gated_tools_say_so(self) -> None:
        """A write tool listed without its condition reads as always available."""
        gated = {"label_address", "record_note"}
        for entry in TOOLS:
            if entry.split(" ")[0] in gated:
                assert "--writable" in entry, f"{entry} does not say it is gated"
