"""Cross-chain matching, scored against known true pairings.

An exchange takes ETH on one chain and pays BTC out on another. Nothing links
the two on-chain: there is no shared identifier, no reference, no counterparty
in common. The match is made from *time and value* — a payout in the right
window, of about the right size once the service's cut is removed.

That is a weak signal used to make a strong claim, which is why measuring it
matters more here than anywhere else in this project. The reported answer is
usually the top-ranked candidate out of dozens, and the failure mode is not
"no answer" but a confident wrong one.

The worlds below are built with the true payout known, alongside decoys
constructed to be *hard*: right window, similar value, plausible shape. What is
scored is whether the true payout ranks first, and — more importantly — whether
the analyzer's confidence tracks whether it should be believed.

I claimed earlier that this needed a public real-world case to measure. That
was wrong: the same synthetic-ground-truth approach used for clustering, change
detection, and peel traversal applies here, and a synthetic world is actually
*better* for this one because the decoys can be made adversarial on purpose.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import ClassVar

import pytest

from chainscope.analysis.xchain import (
    Calibration,
    CrossChainMatcher,
    PayoutCandidate,
    calibrate,
)
from chainscope.core.chainid import ETHEREUM
from chainscope.core.units import Amount
from chainscope.pricing.base import Quote

SATS = 10**8
BASE_TIME = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class Prices:
    """A fixed rate, returned as the real :class:`Quote`.

    Using the actual type rather than a stand-in: an earlier version here was a
    hand-rolled object missing ``.rate``, and every ranking test failed on an
    attribute error that had nothing to do with matching. Real price movement is
    a separate error source, held constant so the matching is what is measured.
    """

    def __init__(self, rate: str = "0.05") -> None:
        self.value = Decimal(rate)

    def rate(self, base: str, quote: str, when: datetime) -> Quote:
        return Quote(
            base=base,
            quote=quote,
            rate=self.value,
            at=when,
            source="fixture",
            derivation="direct",
        )


@dataclass
class World:
    candidates: list[PayoutCandidate] = field(default_factory=list)
    truth: str = ""


class _Ctx:
    """A stand-in for `Context`, carrying what `_result` actually reads.

    It had only `evidence()`. `_result` now also records the chain and the
    limits in `params` --- so a result can say which chain it ran on and what
    cap truncated it --- and a double narrower than the thing it stands in for
    fails on the first change to that thing.
    """

    chain = ETHEREUM
    limits: ClassVar[dict[str, int]] = {}

    def evidence(self) -> list[object]:
        return []


class _Scanner:
    def __init__(self, world: World) -> None:
        self.world = world

    def outputs_between(self, *args, **kwargs) -> list[PayoutCandidate]:
        """The single method the analyzer calls.

        Named to match rather than guessed at: an earlier version of this stub
        offered `candidates`/`payouts`/`outputs_in_window`, none of which the
        analyzer looks for, and every ranking test failed on a missing
        attribute rather than on anything about the matching.
        """
        return list(self.world.candidates)


def payout(
    name: str,
    btc: Decimal,
    *,
    delay: int,
    payer_txs: int = 5000,
    recipient_txs: int = 0,
    outputs: int = 2,
) -> PayoutCandidate:
    return PayoutCandidate(
        txid=name,
        index=0,
        amount=Amount(int(btc * SATS), 8, "BTC"),
        recipient=f"bc1q{name}",
        payer=f"bc1qhot{payer_txs}",
        block=800_000 + delay // 600,
        block_time=BASE_TIME + timedelta(seconds=delay),
        delay_seconds=delay,
        payer_tx_count=payer_txs,
        recipient_tx_count=recipient_txs,
        output_count=outputs,
    )


def build_world(
    *,
    sent_eth: str = "250",
    rate: str = "0.05",
    discount_pct: str = "2.95",
    decoys: int = 30,
    seed: int = 7,
) -> World:
    """One true payout plus adversarial decoys.

    The decoys are not noise. They sit in the same window, come from the same
    service-scale payer, and carry values spread around the true one --- which
    is what a busy exchange hot wallet actually looks like.
    """
    rng = random.Random(seed)
    expected = Decimal(sent_eth) * Decimal(rate)
    true_amount = expected * (1 - Decimal(discount_pct) / 100)

    world = World(truth="true-payout")
    world.candidates.append(
        payout("true-payout", true_amount, delay=1180, recipient_txs=0, outputs=2)
    )

    for i in range(decoys):
        # Values within +/-15% of expected: close enough that a naive nearest
        # match has to work for its answer.
        factor = Decimal(str(1 + rng.uniform(-0.15, 0.15)))
        world.candidates.append(
            payout(
                f"decoy{i}",
                (expected * factor).quantize(Decimal("0.00000001")),
                delay=rng.randint(310, 2690),
                recipient_txs=rng.choice([0, 0, 3, 40]),
                outputs=rng.choice([2, 2, 3, 8]),
            )
        )
    return world


def rank(world: World, **kw) -> list[str]:
    """Candidate txids in the order the analyzer ranks them."""
    result = CrossChainMatcher(Prices(kw.pop("rate", "0.05")), _Scanner(world)).run(
        _Ctx(),
        amount=kw.pop("amount", "250"),
        asset="ETH",
        at=BASE_TIME,
        target_asset="BTC",
        top=len(world.candidates),
        **kw,
    )
    return [h.data.get("txid", "") for h in result.hypotheses]


class TestItFindsTheRealPayout:
    def test_the_true_payout_ranks_first(self):
        world = build_world()
        assert rank(world)[0] == world.truth

    @pytest.mark.parametrize("discount", ["0.5", "1.75", "2.95", "4.2"])
    def test_across_the_plausible_discount_range(self, discount):
        world = build_world(discount_pct=discount)
        assert rank(world)[0] == world.truth

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    def test_it_is_not_one_lucky_arrangement_of_decoys(self, seed):
        world = build_world(seed=seed)
        assert rank(world)[0] == world.truth

    def test_it_holds_with_a_hundred_decoys(self):
        """A busy hot wallet pays out constantly; thirty is optimistic."""
        world = build_world(decoys=100)
        assert rank(world)[0] == world.truth


class TestCalibrationIsTheRealEvidence:
    def test_consistent_discounts_across_independent_swaps(self):
        """A spread this tight across separate swaps is far harder to explain
        by coincidence than any single match. It is the strongest thing this
        technique produces."""
        prices = Prices("0.05")
        cases = [
            (
                Decimal("250"),
                "ETH",
                BASE_TIME,
                Decimal("250") * Decimal("0.05") * Decimal("0.97076"),
            ),
            (
                Decimal("1000"),
                "ETH",
                BASE_TIME,
                Decimal("1000") * Decimal("0.05") * Decimal("0.97018"),
            ),
            (
                Decimal("40"),
                "ETH",
                BASE_TIME,
                Decimal("40") * Decimal("0.05") * Decimal("0.97052"),
            ),
        ]
        calibration = calibrate(cases, "BTC", prices)
        assert calibration.samples == 3
        assert calibration.is_consistent
        assert Decimal("2.9") < calibration.mean_discount < Decimal("3.0")

    def test_scattered_discounts_are_reported_as_inconsistent(self):
        """Three swaps at wildly different rates are not one service, and
        saying so is the point of measuring the spread."""
        prices = Prices("0.05")
        cases = [
            (
                Decimal("100"),
                "ETH",
                BASE_TIME,
                Decimal("100") * Decimal("0.05") * Decimal("0.99"),
            ),
            (
                Decimal("100"),
                "ETH",
                BASE_TIME,
                Decimal("100") * Decimal("0.05") * Decimal("0.94"),
            ),
            (
                Decimal("100"),
                "ETH",
                BASE_TIME,
                Decimal("100") * Decimal("0.05") * Decimal("0.90"),
            ),
        ]
        assert not calibrate(cases, "BTC", prices).is_consistent

    def test_calibration_narrows_the_search(self):
        """The band converts "somewhere below spot, we think" into a range."""
        band = Calibration(Decimal("2.95"), Decimal("0.06"), 3).band(Decimal("0.5"))
        assert band == (Decimal("2.45"), Decimal("3.45"))

    def test_no_usable_cases_is_an_error_not_an_empty_calibration(self):
        with pytest.raises(ValueError, match="no usable cases"):
            calibrate([], "BTC", Prices())


class TestWhereItShouldNotBeBelieved:
    def test_a_missing_true_payout_still_returns_something(self):
        """The failure mode is not "no answer" --- it is a confident wrong one.
        Ranking decoys is what it does when the real payout is not in the
        window at all, and nothing about the output looks different."""
        world = build_world()
        world.candidates = [c for c in world.candidates if c.txid != world.truth]
        ranked = rank(world)
        assert ranked and ranked[0].startswith("decoy")

    def test_two_candidates_at_the_same_value_and_time_are_not_separable(self):
        """Constructed to be genuinely ambiguous. The honest outcome is that
        the top two score alike, not that one is picked and presented as the
        answer."""
        world = build_world(decoys=0)
        true_candidate = world.candidates[0]
        world.candidates.append(
            payout(
                "twin",
                true_candidate.amount.decimal,
                delay=true_candidate.delay_seconds + 5,
                recipient_txs=0,
                outputs=2,
            )
        )
        result = CrossChainMatcher(Prices(), _Scanner(world)).run(
            _Ctx(), amount="250", asset="ETH", at=BASE_TIME, target_asset="BTC", top=2
        )
        scores = [sum(f.contribution for f in h.factors) for h in result.hypotheses]
        assert len(scores) == 2
        assert abs(scores[0] - scores[1]) < 1.0, "a coin flip presented as a ranking"

    def test_a_claim_never_exceeds_medium(self):
        """Time and value are circumstantial. They narrow a hypothesis; they
        cannot confirm one."""
        from chainscope.core.attribution import Confidence

        result = CrossChainMatcher(Prices(), _Scanner(build_world())).run(
            _Ctx(), amount="250", asset="ETH", at=BASE_TIME, target_asset="BTC"
        )
        for hypothesis in result.hypotheses:
            assert hypothesis.confidence <= Confidence.MEDIUM

    def test_an_empty_window_yields_no_hypotheses(self):
        assert rank(World()) == []


class TestInputValidation:
    def test_a_naive_timestamp_is_refused(self):
        """An hour of drift is a different window and a different answer."""
        with pytest.raises(ValueError, match="timezone-aware"):
            CrossChainMatcher(Prices(), _Scanner(build_world())).run(
                _Ctx(), amount="250", asset="ETH", at=datetime(2026, 3, 1, 12, 0)
            )

    def test_missing_arguments_are_refused(self):
        with pytest.raises(ValueError, match="needs"):
            CrossChainMatcher(Prices(), _Scanner(build_world())).run(_Ctx(), amount="250")
