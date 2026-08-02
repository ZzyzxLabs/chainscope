"""Probing detection, scored against ordinary activity.

The recorded sequences are unambiguous once you see them --- ``5, 10, 20, 30,
50, 75, 100, 125, 150, 175`` ETH into one exchange deposit address --- so the
interesting question is not whether the detector finds them. It is **how often
ordinary activity produces the same shape**, because a rule that fires on
normal behaviour fills a case file with confident coincidences, and a
coincidence recorded beside a finding is indistinguishable from it later.

So the negatives here outnumber the positives, and the false-positive rate is
measured against random amounts, regular payments, decreasing runs, and
accumulation --- not asserted.
"""

from __future__ import annotations

import math
import random

import pytest

from chainscope.analysis.probing import (
    MIN_ESCALATION_GROWTH,
    MIN_ESCALATION_STEPS,
    MIN_TEST_RATIO,
    detect_probes,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

SEED = 20260803
ETH = 10**18


def transfers(amounts, *, sender="0xoperator", recipient="0xdeposit", start=1000):
    return [
        Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, f"0x{start + i:064x}"),
            sender=Address(ETHEREUM, sender, sender),
            recipient=Address(ETHEREUM, recipient, recipient),
            amount=Amount(raw, 18, "ETH"),
            kind=TransferKind.NATIVE,
            block=start + i,
            index=0,
        )
        for i, raw in enumerate(amounts)
    ]


#: The TradeOgre sequence, verbatim.
TRADEOGRE = [n * ETH for n in (5, 10, 20, 30, 50, 75, 100, 125, 150, 175)]


class TestTheRecordedSequences:
    def test_the_tradeogre_escalation_is_found(self):
        found = detect_probes(transfers(TRADEOGRE))
        assert len(found) == 1
        assert found[0].kind == "escalation"
        assert found[0].steps == 10

    def test_it_reports_how_unlikely_that_ordering_is(self):
        probe = detect_probes(transfers(TRADEOGRE))[0]
        assert probe.chance == pytest.approx(1 / math.factorial(10))
        assert probe.chance < 1e-6

    def test_the_hitbtc_shape_is_too_short_to_report(self):
        """1, 7, 10 ETH. Three amounts arrive in order one time in six --- the
        notes could call it a probe because they had the rest of the case; this
        function has three numbers and must not."""
        assert detect_probes(transfers([1 * ETH, 7 * ETH, 10 * ETH])) == []

    def test_the_tornado_test_then_commit_is_found(self):
        """0.01 ETH, then the whole balance."""
        found = detect_probes(transfers([ETH // 100, 9 * ETH]))
        assert len(found) == 1
        assert found[0].kind == "test-then-commit"
        assert found[0].growth == pytest.approx(900)

    def test_a_long_escalation_reaches_medium(self):
        assert detect_probes(transfers(TRADEOGRE))[0].confidence is Confidence.MEDIUM

    def test_and_never_more_than_medium(self):
        """A desk scaling into a position produces the same shape. What differs
        is context this function cannot see."""
        huge = [n * ETH for n in range(1, 30)]
        assert detect_probes(transfers(huge))[0].confidence <= Confidence.MEDIUM


class TestFalsePositivesAgainstOrdinaryActivity:
    """The measurement that decides whether this is usable."""

    def _rate(self, make, trials=400):
        rng = random.Random(SEED)
        hits = 0
        for i in range(trials):
            rows = transfers(make(rng), recipient=f"0xdest{i:04d}")
            if detect_probes(rows):
                hits += 1
        return hits / trials

    def test_random_amounts_almost_never_fire(self):
        """Ten random payments to one counterparty. The number that matters:
        with a few hundred counterparties in a case, this is how many
        coincidences land in the report."""
        rate = self._rate(lambda r: [r.randint(1, 100) * ETH for _ in range(10)])
        assert rate < 0.05, f"false positive rate {rate:.1%}"

    def test_regular_equal_payments_never_fire(self):
        """A salary or a subscription. Equal amounts break the run, which is
        why ties are not admitted."""
        assert self._rate(lambda r: [10 * ETH] * 12) == 0.0

    def test_decreasing_runs_never_fire(self):
        assert self._rate(lambda r: [n * ETH for n in range(20, 5, -1)]) == 0.0

    def test_noisy_accumulation_rarely_fires(self):
        """Amounts trending up but not monotonic --- the common honest case."""
        rate = self._rate(
            lambda r: [int((10 + i * 2 + r.uniform(-6, 6)) * ETH) for i in range(12)]
        )
        assert rate < 0.15, f"false positive rate {rate:.1%}"

    def test_two_ordinary_payments_of_different_sizes_do_not_look_like_a_test(self):
        """1 ETH then 50 ETH is two payments. The ratio floor is what keeps
        test-then-commit from firing on every pair of unequal transfers."""
        assert detect_probes(transfers([ETH, 50 * ETH])) == []


class TestWhyTheThresholdIsFive:
    """Chance of n amounts arriving sorted is 1/n!, and intuition is bad at
    factorials. Four is 4.2%; five is 0.83%. With a few hundred counterparties,
    the first produces a handful of confident coincidences and the second about
    one."""

    @pytest.mark.parametrize(
        ("length", "expected"),
        [(3, 1 / 6), (4, 1 / 24), (5, 1 / 120), (10, 1 / 3628800)],
    )
    def test_the_null_model_is_what_it_claims(self, length, expected):
        rows = transfers([n * ETH for n in range(1, length + 1)])
        # min_growth off, to isolate the null model from the threshold that
        # actually gates detection.
        probe = detect_probes(rows, min_steps=3, min_growth=0)[0]
        assert probe.chance == pytest.approx(expected)

    def test_empirically_a_shuffle_sorts_that_often(self):
        """The 1/n! model checked against actual shuffles rather than trusted."""
        rng = random.Random(SEED)
        for length in (3, 4, 5):
            hits = sum(
                1
                for _ in range(20000)
                if (lambda s: s == sorted(s))(rng.sample(range(1000), length))
            )
            assert hits / 20000 == pytest.approx(1 / math.factorial(length), abs=0.01)

    def test_four_steps_is_below_the_default_floor(self):
        assert detect_probes(transfers([n * ETH for n in (1, 2, 4, 8)])) == []

    def test_five_steps_clears_it(self):
        assert len(detect_probes(transfers([n * ETH for n in (1, 2, 4, 8, 16)]))) == 1

    def test_a_lower_floor_is_refused_outright(self):
        with pytest.raises(ValueError, match="min_steps"):
            detect_probes([], min_steps=2)


class TestWhatItGroupsAndSeparates:
    def test_two_assets_are_not_one_sequence(self):
        """Amounts are compared to each other, so mixing assets compares
        numbers whose units differ."""
        usdc = [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{9000 + i:064x}"),
                sender=Address(ETHEREUM, "0xoperator", "0xoperator"),
                recipient=Address(ETHEREUM, "0xdeposit", "0xdeposit"),
                amount=Amount(raw, 6, "USDC"),
                kind=TransferKind.TOKEN,
                block=9000 + i,
                index=0,
                asset=Address(ETHEREUM, "0xusdc", "0xusdc"),
            )
            for i, raw in enumerate([1, 2, 3])
        ]
        # Three ETH steps plus three USDC steps must not become one six-step run.
        assert detect_probes(transfers([ETH, 2 * ETH, 3 * ETH]) + usdc) == []

    def test_two_destinations_are_two_sequences(self):
        ladder = [n * ETH for n in (1, 2, 5, 12, 30, 80)]
        rows = transfers(ladder, recipient="0xa")
        rows += transfers(ladder, recipient="0xb", start=5000)
        assert {p.destination for p in detect_probes(rows)} == {"0xa", "0xb"}

    def test_order_is_by_block_not_by_list_position(self):
        rows = transfers([n * ETH for n in (80, 30, 12, 5, 2)])
        for i, row in enumerate(rows):
            object.__setattr__(row, "block", 1000 - i)
        assert len(detect_probes(rows)) == 1

    def test_an_interrupted_run_reports_only_the_run(self):
        """Escalation, then a drop, then more. The claim is about the run that
        actually happened, not the list it sat in."""
        rows = transfers([n * ETH for n in (1, 2, 5, 12, 30, 80, 1, 2)])
        probe = detect_probes(rows)[0]
        assert probe.steps == 6

    def test_zero_amounts_are_not_steps(self):
        """A contract call carrying no value is not a test payment."""
        assert detect_probes(transfers([0, 0, ETH, 2 * ETH, 3 * ETH])) == []


class TestGrowthIsWhatActuallySeparatesThem:
    """The threshold the measurement forced.

    `1/n!` assumes the amounts are a random permutation. Real payment streams
    drift upward, so five-step increasing runs are common in them --- measured
    at 38% of counterparties. Those runs grow 2.3x on median and never past
    6.5x; the recorded TradeOgre probe grows 35x. Length says a run happened;
    growth says it was a probe."""

    def test_a_long_run_with_no_reach_is_not_a_probe(self):
        """Ten steps, but from 100 to 109 ETH. Somebody sending slightly more
        each time is not testing a route."""
        assert detect_probes(transfers([n * ETH for n in range(100, 110)])) == []

    def test_the_recorded_probe_clears_it_with_room(self):
        probe = detect_probes(transfers(TRADEOGRE))[0]
        assert probe.growth == pytest.approx(35.0)
        assert probe.growth > MIN_ESCALATION_GROWTH * 4

    def test_the_worst_measured_false_positive_is_below_the_threshold(self):
        """Noisy accumulation topped out at 6.5x across four thousand trials."""
        assert MIN_ESCALATION_GROWTH > 6.5

    def test_lowering_it_reintroduces_the_false_positives(self):
        """The threshold is load-bearing, not decoration: without it the rate
        goes back to what the harness measured."""
        rng = random.Random(SEED)
        hits = 0
        for i in range(200):
            amounts = [int((10 + j * 2 + rng.uniform(-6, 6)) * ETH) for j in range(12)]
            if detect_probes(transfers(amounts, recipient=f"0xd{i}"), min_growth=0):
                hits += 1
        assert hits / 200 > 0.25


class TestTheClaim:
    def test_it_admits_a_trading_desk_looks_the_same(self):
        probe = detect_probes(transfers(TRADEOGRE))[0]
        assert "scaling into a position" in probe.summary()

    def test_it_states_the_odds_rather_than_asserting_intent(self):
        assert "chance about" in detect_probes(transfers(TRADEOGRE))[0].summary()

    def test_the_claim_is_attached_to_the_destination(self):
        claim = detect_probes(transfers(TRADEOGRE))[0].attribution(ETHEREUM)
        assert claim.address == "0xdeposit"
        assert claim.source

    def test_a_test_then_commit_names_the_ratio(self):
        probe = detect_probes(transfers([ETH // 1000, 10 * ETH]))[0]
        assert "test payment" in probe.summary()
        assert probe.growth >= MIN_TEST_RATIO

    def test_the_default_floor_is_what_the_module_documents(self):
        assert MIN_ESCALATION_STEPS == 5
