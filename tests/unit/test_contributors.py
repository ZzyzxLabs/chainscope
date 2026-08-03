"""Whose money is in this total.

The last of the four noise filters a methodology written from real cases lists,
and the one that separates a defensible total from an inflated one.

The situation it comes from: a deposit address is identified as the subject's,
its inbound total is computed, and the figure goes in the report. That address
had also received **3 ETH from a completely unrelated party** --- an address
with 122 transactions of its own, funded from a different 500 ETH, and itself a
victim of the same poisoning campaign. Included, it inflates "the subject sent N
ETH to this service" by exactly 3 ETH, and nothing about the number looks wrong.

A deposit address is a destination, not a private channel. Anybody may pay it.

The two properties worth protecting are both refusals:

* nothing is subtracted --- producing "the corrected total" hides the judgement
  inside a number, and the judgement is the reader's;
* `unlinked` never means `unrelated` --- an address related through a hop nobody
  fetched sits in that bucket beside a genuine stranger, and absence cannot tell
  them apart.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chainscope.analysis.contributors import Link, contributors, findings

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

SEED = "0x" + "e" * 40
RELAY = "0x" + "6" * 40
DEPOSIT = "0x" + "d" * 40
STRANGER = "0x" + "a" * 40
ELSEWHERE = "0x" + "f" * 40
EXCHANGE = "0x" + "c" * 40


def _t(sender: str, recipient: str, minutes: int, eth: float) -> object:
    return SimpleNamespace(
        sender=SimpleNamespace(key=sender),
        recipient=SimpleNamespace(key=recipient),
        timestamp=T0 + timedelta(minutes=minutes),
        asset=None,
        amount=SimpleNamespace(raw=int(eth * 10**18), symbol="ETH", decimals=18),
        tx=SimpleNamespace(hash=f"{sender}{recipient}{minutes}"),
    )


#: The case, reduced: 750 ETH through a relay, and 3 ETH that wandered in.
CASE = [
    _t(SEED, RELAY, 1, 750),
    _t(RELAY, DEPOSIT, 2, 750),
    _t(ELSEWHERE, STRANGER, 1, 500),
    _t(STRANGER, DEPOSIT, 3, 3),
]


class TestTheCase:
    def test_the_stray_payment_is_separated(self) -> None:
        inflow = contributors(CASE, DEPOSIT, SEED)
        assert inflow.total == 753 * 10**18
        assert inflow.attributable == 750 * 10**18
        assert inflow.unlinked == 3 * 10**18

    def test_the_relay_is_reachable_from_the_seed(self) -> None:
        inflow = contributors(CASE, DEPOSIT, SEED)
        relay = next(c for c in inflow.contributions if c.address == RELAY)
        assert relay.link == Link.REACHABLE
        assert relay.is_attributable

    def test_the_stranger_is_not(self) -> None:
        inflow = contributors(CASE, DEPOSIT, SEED)
        stranger = next(c for c in inflow.contributions if c.address == STRANGER)
        assert stranger.link == Link.UNLINKED
        assert not stranger.is_attributable

    def test_nothing_is_subtracted(self) -> None:
        # The total still reports everything that arrived. Producing "the
        # corrected figure" would hide the judgement inside a number.
        inflow = contributors(CASE, DEPOSIT, SEED)
        assert inflow.total == inflow.attributable + inflow.unlinked

    def test_the_summary_says_which_figure_to_quote(self) -> None:
        summary = contributors(CASE, DEPOSIT, SEED).summary()
        assert "not the sum" in summary

    def test_amounts_are_readable(self) -> None:
        # 753000000000000000000 is not a number anybody checks. Decimals are
        # carried rather than assumed --- rendering at the wrong scale is the
        # defect that put 0.000000 on the dashboard where 1,000 USDC belonged.
        assert "753 ETH" in contributors(CASE, DEPOSIT, SEED).summary()


class TestTheSubjectItself:
    def test_a_direct_payment_is_self(self) -> None:
        rows = [_t(SEED, DEPOSIT, 1, 10)]
        inflow = contributors(rows, DEPOSIT, SEED)
        assert inflow.contributions[0].link == Link.SELF
        assert inflow.attributable == inflow.total

    def test_an_all_clean_inflow_says_so(self) -> None:
        rows = [_t(SEED, DEPOSIT, 1, 10)]
        assert "all of it from" in contributors(rows, DEPOSIT, SEED).summary()


class TestSharedFunding:
    def test_a_shared_first_funder_is_reported_separately(self) -> None:
        """Weaker than a route, and it must not be counted as one.

        `chainscope.analysis.funding` measures what happens when this signal is
        trusted without a service guard: precision drops to 0.7%, because an
        exchange funds thousands of unrelated customers.
        """
        rows = [
            _t(EXCHANGE, SEED, 1, 100),
            _t(EXCHANGE, STRANGER, 2, 100),
            _t(SEED, DEPOSIT, 3, 10),
            _t(STRANGER, DEPOSIT, 4, 5),
        ]
        inflow = contributors(rows, DEPOSIT, SEED)
        stranger = next(c for c in inflow.contributions if c.address == STRANGER)
        assert stranger.link == Link.CO_FUNDED

    def test_and_is_not_counted_as_the_subjects(self) -> None:
        rows = [
            _t(EXCHANGE, SEED, 1, 100),
            _t(EXCHANGE, STRANGER, 2, 100),
            _t(SEED, DEPOSIT, 3, 10),
            _t(STRANGER, DEPOSIT, 4, 5),
        ]
        inflow = contributors(rows, DEPOSIT, SEED)
        assert inflow.attributable == 10 * 10**18

    def test_it_says_why_that_is_weak(self) -> None:
        rows = [
            _t(EXCHANGE, SEED, 1, 100),
            _t(EXCHANGE, STRANGER, 2, 100),
            _t(SEED, DEPOSIT, 3, 10),
            _t(STRANGER, DEPOSIT, 4, 5),
        ]
        inflow = contributors(rows, DEPOSIT, SEED)
        stranger = next(c for c in inflow.contributions if c.address == STRANGER)
        assert "thousands of unrelated customers" in stranger.detail


class TestUnlinkedIsNotUnrelated:
    def test_a_hop_beyond_the_bound_lands_in_unlinked(self) -> None:
        """The property that makes the name matter.

        This contributor *is* the subject's money, five hops along. Searched to
        two, it is indistinguishable from a stranger --- so the bucket is named
        for what is true of it, not for what is being guessed.
        """
        # A single line: SEED -> 1 -> 2 -> 3 -> 4 -> 5 -> DEPOSIT, no shortcuts.
        # The first version had SEED paying every node directly, so there was no
        # long chain to bound --- the fixture, not the code, was wrong.
        hops = [SEED] + ["0x" + f"{n:040x}" for n in range(1, 6)]
        chain = [_t(a, b, i + 1, 10) for i, (a, b) in enumerate(itertools.pairwise(hops))]
        chain.append(_t(hops[-1], DEPOSIT, len(hops), 10))

        near = contributors(chain, DEPOSIT, SEED, max_hops=6)
        assert all(c.link == Link.REACHABLE for c in near.contributions)

        far = contributors(chain, DEPOSIT, SEED, max_hops=2)
        assert all(c.link == Link.UNLINKED for c in far.contributions)

    def test_the_bound_travels_with_the_result(self) -> None:
        inflow = contributors(CASE, DEPOSIT, SEED, max_hops=2)
        assert inflow.hops_searched == 2

    def test_the_finding_states_how_far_anybody_looked(self) -> None:
        inflow = contributors(CASE, DEPOSIT, SEED)
        detail = " ".join(f.detail for f in findings(inflow))
        assert "within 4 hops" in detail

    def test_and_says_to_backtrack_before_excluding(self) -> None:
        # In the case this comes from, what settled it was the third party
        # having 122 transactions of its own and unrelated funding --- not the
        # absence of a link.
        inflow = contributors(CASE, DEPOSIT, SEED)
        detail = " ".join(f.detail for f in findings(inflow))
        assert "Backtrack this address before excluding it" in detail


class TestTheFindings:
    def test_a_clean_inflow_is_not_flagged(self) -> None:
        from chainscope.core.result import Severity

        rows = [_t(SEED, DEPOSIT, 1, 10)]
        found = findings(contributors(rows, DEPOSIT, SEED))
        assert found[0].severity == Severity.INFO

    def test_a_contaminated_one_is(self) -> None:
        from chainscope.core.result import Severity

        found = findings(contributors(CASE, DEPOSIT, SEED))
        assert found[0].severity == Severity.IMPORTANT

    def test_an_empty_target_is_reported_as_a_fact_about_the_store(self) -> None:
        found = findings(contributors(CASE, "0x" + "9" * 40, SEED))
        assert "about the store, not about" in found[0].detail

    def test_the_largest_contributor_comes_first(self) -> None:
        # The one that most changes the total is the one whose classification
        # most needs checking.
        amounts = [c.amount for c in contributors(CASE, DEPOSIT, SEED).contributions]
        assert amounts == sorted(amounts, reverse=True)
