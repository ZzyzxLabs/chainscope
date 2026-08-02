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
    """Names of functions decorated with `@server.tool(...)`.

    `AsyncFunctionDef` as well as `FunctionDef`: the MCP SDK accepts both, and
    a check that silently skipped async tools would go stale the first time one
    is written --- which is this file's own failure mode.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(call, ast.Attribute) and call.attr == "tool":
                found.add(node.name)
    return found


def write_gated() -> set[str]:
    """Tool names defined inside the server's `if config.writable:` block."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Attribute) and test.attr == "writable"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(inner.name)
    return found & registered()


def listed() -> set[str]:
    # The list carries "(only with --writable)" for gated tools; that qualifier
    # is for a human reading `doctor` and is not part of the name.
    return {entry.split(" ")[0] for entry in TOOLS}


def test_the_check_found_something() -> None:
    """Both parametrised classes below iterate over discovered names.

    If discovery returns nothing --- a refactor moves the decorator, the AST
    shape changes --- every one of those cases collects zero tests and the file
    passes green while checking nothing. That is the exact way a currency check
    stops being one, and it is what this file was written about.
    """
    assert registered(), "no @server.tool functions found; the parser has gone stale"
    assert listed(), "TOOLS is empty"


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
        """A write tool listed without its condition reads as always available.

        The gated set is read from the source rather than typed here: a third
        write tool added inside the same `if config.writable:` block would
        otherwise be listed as unconditional and nothing would notice.
        """
        gated = write_gated()
        assert gated, "no tools found inside the writable branch"
        for name in gated:
            entry = next(e for e in TOOLS if e.split(" ")[0] == name)
            assert "--writable" in entry, f"{entry} does not say it is gated"

    def test_ungated_tools_do_not_claim_to_be(self) -> None:
        # The other direction: a read tool marked --writable would send an
        # operator looking for a flag they do not need.
        for entry in TOOLS:
            if "--writable" in entry:
                assert entry.split(" ")[0] in write_gated()
