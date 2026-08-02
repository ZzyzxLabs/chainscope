"""An enumeration must say how sure it is, every time.

§1 of `docs/needs.md` is built on three observed failures with one shape: the
tool had an answer, the answer looked normal, and nothing said it might be
wrong. An archive endpoint returned twelve of thirteen logs with HTTP 200 and
one withdrawal address went missing from a real case.

`Router.corroborate` was the fix and it was **opt-in per call site**. One of
nine analyzers called it. The other seven went through `dispatch` and got a
bare list --- no record of which source answered, and no way for a reader to
tell a checked answer from an unchecked one. An investigator who did not know
the feature existed got the weaker answer and no sign of it, which is the same
silence the feature was built against.

So what is pinned here is not "corroboration happens". It cannot always: one
provider may be all there is for a chain. It is that **the answer always
carries a statement about its own completeness.**
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.providers.base import Capability, CostTier, Provider, ProviderError
from chainscope.providers.router import Router


class Fake(Provider):
    """A provider that returns exactly what it is told to."""

    def __init__(self, name: str, rows: list[str], *, fails: bool = False) -> None:
        self.name = name
        self.cost = CostTier.FREE_PUBLIC
        self._rows = rows
        self._fails = fails
        self.calls = 0

    def supports(self, chain: Any, capability: Capability) -> bool:
        return bool(capability & Capability.ADDRESS_HISTORY)

    def capabilities(self, chain: Any) -> Capability:
        return Capability.ADDRESS_HISTORY

    def address_history(self, chain: Any, address: str, **kw: Any) -> list[str]:
        self.calls += 1
        if self._fails:
            raise ProviderError(f"{self.name} is down")
        return list(self._rows)


def rows_of(provider: Provider) -> list[str]:
    return provider.address_history(ETHEREUM, "0xabc")  # type: ignore[attr-defined]


def enumerate_with(*providers: Provider, corroborate: bool = True) -> Any:
    router = Router(list(providers), corroborate_enumerations=corroborate)
    return router.enumerate(ETHEREUM, Capability.ADDRESS_HISTORY, rows_of, key=lambda r: r)


class TestASingleSourceAnswerSaysSo:
    def test_one_provider_available(self) -> None:
        # Not a failure --- it is the ordinary case on a chain with one
        # explorer. What matters is that the result does not read as checked.
        found = enumerate_with(Fake("only", ["a", "b"]))
        assert found.rows == ["a", "b"]
        assert not found.corroborated
        assert "not corroborated" in found.summary()

    def test_the_caller_asked_for_one(self) -> None:
        first, second = Fake("a", ["x"]), Fake("b", ["x"])
        found = enumerate_with(first, second, corroborate=False)
        assert not found.corroborated
        assert "not corroborated" in found.summary()
        # And it really only asked once.
        assert first.calls + second.calls == 1

    def test_it_names_which_source_answered(self) -> None:
        found = enumerate_with(Fake("etherscan", ["a"]))
        assert found.sources == ("etherscan",)
        assert "etherscan" in found.summary()


class TestTwoSourcesAgreeing:
    def test_the_answer_is_marked_corroborated(self) -> None:
        found = enumerate_with(Fake("a", ["x", "y"]), Fake("b", ["y", "x"]))
        assert found.corroborated
        assert not found.disagreed
        assert sorted(found.rows) == ["x", "y"]

    def test_both_were_actually_asked(self) -> None:
        first, second = Fake("a", ["x"]), Fake("b", ["x"])
        enumerate_with(first, second)
        assert first.calls == 1 and second.calls == 1


class TestTwoSourcesDisagreeing:
    def test_the_union_is_returned(self) -> None:
        # For an enumeration a row one source missed is still a row, and the
        # larger answer is usually the complete one.
        found = enumerate_with(Fake("a", ["x", "y"]), Fake("b", ["x"]))
        assert sorted(found.rows) == ["x", "y"]

    def test_disagreement_is_not_reported_as_agreement(self) -> None:
        found = enumerate_with(Fake("a", ["x", "y"]), Fake("b", ["x"]))
        assert found.disagreed
        assert not found.corroborated

    def test_the_summary_refuses_to_pick_a_winner(self) -> None:
        # Which source is right is not decidable here: the larger result is
        # usually complete, but a provider double-counting also produces more.
        found = enumerate_with(Fake("a", ["x", "y"]), Fake("b", ["x"]))
        assert "not decidable" in found.summary()
        assert "a alone saw 1" in found.summary()


class TestFailures:
    def test_a_dead_provider_falls_back_and_the_result_is_single_source(self) -> None:
        found = enumerate_with(Fake("dead", [], fails=True), Fake("live", ["x"]))
        assert found.rows == ["x"]
        assert not found.corroborated
        assert found.failures

    def test_no_provider_at_all_raises_rather_than_returning_nothing(self) -> None:
        from chainscope.providers.router import NoProviderError

        with pytest.raises(NoProviderError):
            enumerate_with()


class TestTheAnalyzersUseIt:
    """The part that was actually missing: reaching it from the analyzers."""

    def test_history_of_surfaces_the_completeness_note(self) -> None:
        from chainscope.analysis.base import Context, history_of

        ctx = Context(chain=ETHEREUM, router=Router([Fake("only", ["a"])]), limits={})
        rows, notes = history_of(ctx, rows_of)
        assert rows == ["a"]
        assert notes and "not corroborated" in notes[0]

    def test_a_corroborated_history_adds_no_noise(self) -> None:
        # A warning that fires every run is one people stop reading, so the
        # checked case says nothing.
        from chainscope.analysis.base import Context, history_of

        router = Router([Fake("a", ["x"]), Fake("b", ["x"])])
        ctx = Context(chain=ETHEREUM, router=router, limits={})
        _, notes = history_of(ctx, rows_of)
        assert notes == []

    def test_disagreement_reaches_the_caller(self) -> None:
        from chainscope.analysis.base import Context, history_of

        router = Router([Fake("a", ["x", "y"]), Fake("b", ["x"])])
        ctx = Context(chain=ETHEREUM, router=router, limits={})
        rows, notes = history_of(ctx, rows_of)
        assert sorted(rows) == ["x", "y"]
        assert notes and "alone saw" in notes[0]
