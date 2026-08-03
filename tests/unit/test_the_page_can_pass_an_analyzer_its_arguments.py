"""An analyzer's declared arguments must survive the trip from the form.

The page renders an input for each name in an analyzer's ``REQUIRES`` and sends
them with the request. `_run_over_store` then called ``instance.run(ctx,
address=..., rows=...)`` and nothing else, so the values were dropped between
the request and the analyzer --- and the analyzer answered "linked_holders
needs `addresses`" about the addresses somebody had just typed into the field
it asked for.

Seven of the fourteen analyzers on offer take an argument, so half the panel
was a form that could not be submitted. Found by filling it in a browser, not
by a test: every existing test called the analyzers directly.

The second test is the one that matters longer term. Forwarding the *whole*
query would have fixed the visible bug and opened a different hole --- a URL
could then set any keyword argument on any analyzer, including ones it never
advertised. Only declared names travel.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from chainscope.core.chainid import ChainId
from chainscope.core.result import Result
from chainscope.server import local

CHAIN = ChainId.evm(1)
SEED = "0x" + "11" * 20


class _Spy:
    """Stands in for a registered analyzer and records what it was handed."""

    name = "spy"
    REQUIRES: ClassVar[tuple[str, ...]] = ("addresses", "source")
    seen: ClassVar[dict[str, Any]] = {}

    def run(self, ctx: Any, **kwargs: Any) -> Result:
        type(self).seen = dict(kwargs)
        return Result(analyzer="spy")


@pytest.fixture
def spying(monkeypatch: pytest.MonkeyPatch) -> type[_Spy]:
    _Spy.seen = {}
    monkeypatch.setattr(
        "chainscope.cli.commands.analyze._discover",
        lambda: ({"spy": _Spy}, []),
    )
    return _Spy


def test_a_declared_argument_reaches_the_analyzer(spying: type[_Spy]) -> None:
    local._run_over_store(
        "spy", [], SEED, CHAIN, SEED, {"addresses": f"{SEED},0xabc", "source": "0xdef"}
    )
    assert spying.seen["addresses"] == f"{SEED},0xabc"
    assert spying.seen["source"] == "0xdef"


def test_an_undeclared_argument_does_not(spying: type[_Spy]) -> None:
    """A query parameter is not a way to set arbitrary keyword arguments."""
    local._run_over_store("spy", [], SEED, CHAIN, SEED, {"addresses": SEED, "provider": "evil"})
    assert "provider" not in spying.seen


def test_nothing_supplied_is_still_a_clean_call(spying: type[_Spy]) -> None:
    """The analyzer's own default and its own error message, not a TypeError."""
    local._run_over_store("spy", [], SEED, CHAIN, SEED, {})
    assert "addresses" not in spying.seen
    assert spying.seen["address"] == SEED
