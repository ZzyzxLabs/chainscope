"""Asking twice, because a short answer looks exactly like a complete one.

This reproduces a measured failure rather than an imagined one. Field notes
from a real multi-chain trace record an archive endpoint's `eth_getLogs`
returning a log when asked for one block and losing it when asked in
five-hundred-block ranges --- HTTP 200, no error, no truncation marker. One
withdrawal address out of thirteen went missing, and the omission was invisible
until a second source was consulted.

Every other guard in this package assumes the provider says something is wrong:
`is_cacheable` reads an error envelope, `ResultTruncated` reads a documented
cap. Neither can fire here. From the outside, a set with a missing element is
just a smaller set.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.providers.base import Capability, CostTier, Provider, ProviderError
from chainscope.providers.router import NoProviderError, Router


class FakeSource(Provider):
    """A provider that returns exactly the rows it was given."""

    ecosystems = frozenset({"eip155"})
    capabilities = Capability.LOGS
    cost = CostTier.FREE_PUBLIC

    def __init__(self, name, rows, *, fails=False):
        super().__init__()
        self.name = name
        self.chains = frozenset({ETHEREUM})
        self.rows = rows
        self.fails = fails

    def fetch(self):
        if self.fails:
            raise ProviderError("upstream said no")
        return list(self.rows)


THIRTEEN = [f"0xwithdrawal{i:02d}" for i in range(13)]
#: The same query, five hundred blocks at a time. One row gone, silently.
TWELVE = [a for a in THIRTEEN if a != "0xwithdrawal07"]


def router(*sources):
    return Router(list(sources))


def run(r, *, required=False):
    return r.corroborate(
        ETHEREUM, Capability.LOGS, lambda p: p.fetch(), key=lambda x: x, required=required
    )


class TestTheMissingRow:
    """The case the mechanism exists for."""

    def test_the_dropped_row_is_surfaced(self):
        result = run(router(FakeSource("complete", THIRTEEN), FakeSource("lossy", TWELVE)))
        assert result.disagreed
        assert result.only_in["complete"] == ["0xwithdrawal07"]

    def test_the_union_keeps_it(self):
        """An enumeration is a set: a row one source missed is still a row."""
        result = run(router(FakeSource("complete", THIRTEEN), FakeSource("lossy", TWELVE)))
        assert len(result.rows) == 13

    def test_it_does_not_claim_corroboration(self):
        result = run(router(FakeSource("complete", THIRTEEN), FakeSource("lossy", TWELVE)))
        assert not result.corroborated

    def test_it_refuses_to_pick_a_winner(self):
        """Which source is right is not decidable here. The larger result is
        usually the complete one, but a provider double-counting an internal
        transaction also produces more rows."""
        summary = run(
            router(FakeSource("complete", THIRTEEN), FakeSource("lossy", TWELVE))
        ).summary()
        assert "not decidable" in summary
        assert "double-counting" in summary

    def test_the_direction_of_the_loss_is_visible(self):
        """Which provider was short, not merely that they differed."""
        result = run(router(FakeSource("lossy", TWELVE), FakeSource("complete", THIRTEEN)))
        assert "lossy" not in result.only_in
        assert "complete" in result.only_in


class TestAgreement:
    def test_two_sources_agreeing_is_corroborated(self):
        result = run(router(FakeSource("a", THIRTEEN), FakeSource("b", list(THIRTEEN))))
        assert result.corroborated
        assert not result.disagreed
        assert len(result.rows) == 13

    def test_the_summary_names_both(self):
        summary = run(router(FakeSource("a", THIRTEEN), FakeSource("b", THIRTEEN))).summary()
        assert "a and b" in summary

    def test_two_empty_sources_agree(self):
        """Genuinely nothing is a real answer and must not read as a failure."""
        result = run(router(FakeSource("a", []), FakeSource("b", [])))
        assert result.corroborated
        assert result.rows == []


class TestOneSourceIsNotCorroboration:
    def test_a_lone_provider_still_answers(self):
        result = run(router(FakeSource("only", THIRTEEN)))
        assert len(result.rows) == 13

    def test_but_does_not_claim_to_be_corroborated(self):
        """The distinction has to survive to the caller: "checked against a
        second source" and "asked one source" are different claims."""
        result = run(router(FakeSource("only", THIRTEEN)))
        assert not result.corroborated
        assert result.sources == ("only",)

    def test_the_summary_says_so_plainly(self):
        assert "not corroborated" in run(router(FakeSource("only", THIRTEEN))).summary()

    def test_required_refuses_rather_than_pretending(self):
        with pytest.raises(NoProviderError, match="two independent providers"):
            run(router(FakeSource("only", THIRTEEN)), required=True)

    def test_required_is_satisfied_by_two(self):
        run(router(FakeSource("a", THIRTEEN), FakeSource("b", THIRTEEN)), required=True)


class TestFailures:
    def test_one_failing_source_leaves_a_single_source_answer(self):
        result = run(router(FakeSource("good", THIRTEEN), FakeSource("bad", [], fails=True)))
        assert len(result.rows) == 13
        assert result.sources == ("good",)
        # Not corroborated: one source answered, whatever the other intended.
        assert not result.corroborated
        assert result.failures

    def test_a_failure_is_distinct_from_an_empty_answer(self):
        """ "could not answer" and "answered nothing" are different, and a
        mechanism about completeness cannot blur them."""
        failed = run(router(FakeSource("good", THIRTEEN), FakeSource("bad", [], fails=True)))
        empty = run(router(FakeSource("good", THIRTEEN), FakeSource("quiet", [])))
        assert failed.failures and not empty.failures
        assert empty.sources == ("good", "quiet")

    def test_every_source_failing_raises(self):
        with pytest.raises(ProviderError, match="every provider failed"):
            run(router(FakeSource("a", [], fails=True), FakeSource("b", [], fails=True)))

    def test_no_provider_at_all_raises(self):
        with pytest.raises(NoProviderError):
            run(router())


class TestKeying:
    def test_rows_are_compared_by_key_not_identity(self):
        """Two providers render the same row differently --- different casing,
        a field one omits. Without a key they would disagree about everything."""
        a = FakeSource("a", [{"hash": "0xAB"}])
        b = FakeSource("b", [{"hash": "0xab", "extra": 1}])
        result = Router([a, b]).corroborate(
            ETHEREUM,
            Capability.LOGS,
            lambda p: p.fetch(),
            key=lambda r: r["hash"].lower(),
        )
        assert result.corroborated
        assert len(result.rows) == 1

    def test_duplicates_within_one_source_collapse(self):
        result = run(router(FakeSource("a", ["x", "x", "y"]), FakeSource("b", ["x", "y"])))
        assert result.corroborated
        assert sorted(result.rows) == ["x", "y"]
