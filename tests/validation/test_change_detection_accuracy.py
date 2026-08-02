"""Change-output detection, measured against known ground truth.

Deciding which output of a two-output transaction is change is the step every
peel-chain trace rests on, and it is a *guess*. The published heuristics
(Meiklejohn et al., IMC 2013; Androulaki et al., FC 2013) each have known
counter-examples, and the usual practice is to apply them, print a chain, and
never state the error rate.

So the scenarios below are constructed with the answer known, drawn from the
shapes the literature describes and from what wallets actually do. What is
being measured is not only whether the top choice is right, but whether the
analyzer *knows* when it is guessing --- because a peel chain followed through
a bad guess is worse than one that stops. It looks equally authoritative and is
wrong from that hop onward.

The scoring is deliberately not tuned to make these pass. Where the heuristics
genuinely cannot separate two outputs, the test asserts that the decision is
reported as contested rather than asserting a particular answer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import pytest

from chainscope.analysis.peel import Output, detect_change
from chainscope.core.units import Amount

BTC = 8
SATS = 10**8


def out(index: int, address: str, btc: str, *, script: str = "p2wpkh", seen: int = 0) -> Output:
    return Output(
        index=index,
        address=address,
        amount=Amount(int(Decimal(btc) * SATS), BTC, "BTC"),
        script_type=script,
        recipient_tx_count=seen,
    )


INPUTS = {"bc1qsender"}
SCRIPTS = {"p2wpkh"}


def decide(outputs, inputs=None, scripts=None):
    return detect_change(outputs, inputs or INPUTS, scripts or SCRIPTS)


class TestTheCasesTheHeuristicsAreFor:
    def test_address_reuse_is_decisive(self):
        """The strongest single signal in the literature: an output paying back
        into the input set is change, because a payee has no reason to be
        there."""
        decision = decide(
            [
                out(0, "bc1qpayee", "0.5", seen=0),
                out(1, "bc1qsender", "3.28471", seen=12),
            ]
        )
        assert decision.index == 1
        assert decision.confident

    def test_a_classic_peel_keeps_the_large_side(self):
        """A peel chain pays a little out and carries the rest forward, so the
        large output is change even though both addresses are fresh."""
        decision = decide(
            [
                out(0, "bc1qpayee", "0.5", seen=0),
                out(1, "bc1qchange", "48.71", seen=0),
            ]
        )
        assert decision.index == 1

    def test_a_round_payment_marks_the_other_side_as_change(self):
        """People send 0.5; wallets compute 3.28471. The non-round output is
        the remainder."""
        decision = decide(
            [
                out(0, "bc1qpayee", "0.5", seen=3),
                out(1, "bc1qchange", "3.28471", seen=0),
            ]
        )
        assert decision.index == 1

    def test_a_script_type_mismatch_points_away_from_change(self):
        """A wallet generates change of its own script type. Paying a legacy
        address from a segwit wallet is a payment, not change."""
        decision = decide(
            [
                out(0, "1LegacyPayee", "2.0", script="p2pkh", seen=5),
                out(1, "bc1qchange", "1.13094", script="p2wpkh", seen=0),
            ]
        )
        assert decision.index == 1


class TestItKnowsWhenItIsGuessing:
    def test_two_identical_outputs_are_contested(self):
        """Nothing distinguishes them. Picking one would be an arbitrary
        tiebreak dressed as a finding."""
        decision = decide(
            [
                out(0, "bc1qa", "1.0", seen=0),
                out(1, "bc1qb", "1.0", seen=0),
            ]
        )
        assert not decision.confident

    def test_equal_amounts_do_not_hand_one_side_a_largest_output_bonus(self):
        """max() returns whichever came first, which would award a decisive
        two-point edge invented from nothing."""
        decision = decide(
            [
                out(0, "bc1qa", "1.0", seen=0),
                out(1, "bc1qb", "1.0", seen=4),
            ]
        )
        factors = {f.name: f for f in decision.hypothesis.factors}
        assert not factors["largest_output"].value

    def test_a_single_output_is_not_a_change_decision(self):
        """A sweep has no change. Reporting the only output as change with
        confidence would be a claim about nothing."""
        decision = decide([out(0, "bc1qdest", "5.0", seen=0)])
        assert decision.index == 0
        assert "nothing to distinguish" in decision.hypothesis.claim

    def test_no_outputs_is_speculative_not_an_answer(self):
        from chainscope.core.attribution import Confidence

        decision = decide([])
        assert decision.index is None
        assert decision.hypothesis.confidence is Confidence.SPECULATIVE


class TestKnownCounterExamples:
    """Cases the literature flags as where these heuristics get it wrong.

    Documented rather than papered over. A heuristic whose failure modes are
    written down can be argued with; one whose failures are unknown gets
    believed.
    """

    def test_a_round_change_amount_misleads(self):
        """Change happens to land on a round number, and the payment does not.
        Every value-shaped signal now points the wrong way."""
        decision = decide(
            [
                out(0, "bc1qpayee", "3.28471", seen=0),
                out(1, "bc1qchange", "0.5", seen=0),
            ]
        )
        # It gets this wrong, and that is the point of writing it down.
        assert decision.index == 0

    def test_paying_more_than_you_keep_inverts_the_size_signal(self):
        """A large payment with small change. "Change carries most of the
        value" is a peel-chain assumption, not a general one."""
        decision = decide(
            [
                out(0, "bc1qpayee", "40.0", seen=0),
                out(1, "bc1qchange", "0.03119", seen=0),
            ]
        )
        assert decision.index == 0

    def test_reuse_still_wins_against_the_size_signal(self):
        """When the strong signal and the weak one disagree, the strong one
        should carry it. Reuse is +5; largest is +2."""
        decision = decide(
            [
                out(0, "bc1qpayee", "40.0", seen=0),
                out(1, "bc1qsender", "0.03119", seen=9),
            ]
        )
        assert decision.index == 1


class TestScoringIsInspectable:
    def test_every_factor_is_reported_with_its_weight(self):
        """A ranking nobody can audit is a ranking nobody should act on."""
        decision = decide(
            [out(0, "bc1qpayee", "0.5", seen=0), out(1, "bc1qsender", "3.28471", seen=12)]
        )
        names = {f.name for f in decision.hypothesis.factors}
        assert names == {
            "pays_back_into_input_set",
            "recipient_is_fresh",
            "round_number",
            "script_type_matches_inputs",
            "largest_output",
        }

    def test_the_rejected_output_is_kept_as_an_alternative(self):
        """So a reader can see what the second-best explanation was, and how
        close it came."""
        decision = decide(
            [out(0, "bc1qpayee", "0.5", seen=0), out(1, "bc1qsender", "3.28471", seen=12)]
        )
        assert decision.hypothesis.alternatives

    def test_a_decision_never_claims_more_than_medium(self):
        """This is inference from circumstance. It narrows a hypothesis; it
        does not confirm one."""
        from chainscope.core.attribution import Confidence

        decision = decide(
            [out(0, "bc1qpayee", "0.5", seen=0), out(1, "bc1qsender", "3.28471", seen=12)]
        )
        assert decision.hypothesis.confidence <= Confidence.MEDIUM


class TestMeasuredAccuracy:
    """The headline number, over the scenarios above.

    Held as a floor rather than an exact figure: the point is to notice a
    regression, not to freeze a particular scoring.
    """

    #: (outputs, index of the real change output)
    CASES: ClassVar[list[tuple[list[Output], int]]] = [
        ([out(0, "bc1qpayee", "0.5", seen=0), out(1, "bc1qsender", "3.28", seen=12)], 1),
        ([out(0, "bc1qpayee", "0.5", seen=0), out(1, "bc1qchange", "48.71", seen=0)], 1),
        ([out(0, "bc1qpayee", "0.5", seen=3), out(1, "bc1qchange", "3.28471", seen=0)], 1),
        (
            [
                out(0, "1Payee", "2.0", script="p2pkh", seen=5),
                out(1, "bc1qchange", "1.13094", seen=0),
            ],
            1,
        ),
        ([out(0, "bc1qpayee", "1.0", seen=7), out(1, "bc1qchange", "9.4471", seen=0)], 1),
        ([out(0, "bc1qpayee", "0.25", seen=2), out(1, "bc1qsender", "0.03119", seen=9)], 1),
        # The two known counter-examples, included so the figure is honest.
        ([out(0, "bc1qpayee", "3.28471", seen=0), out(1, "bc1qchange", "0.5", seen=0)], 1),
        ([out(0, "bc1qpayee", "40.0", seen=0), out(1, "bc1qchange", "0.03119", seen=0)], 1),
    ]

    def test_accuracy_over_the_scenario_set(self):
        correct = sum(1 for outs, truth in self.CASES if decide(outs).index == truth)
        accuracy = correct / len(self.CASES)
        # 6 of 8. The two misses are the documented counter-examples above:
        # round change, and a payment larger than the change. Both invert every
        # value-shaped signal at once, and no amount of weighting fixes that
        # without breaking the common case.
        assert accuracy >= 0.75, f"{correct}/{len(self.CASES)}"

    @pytest.mark.parametrize(
        ("outputs", "truth"),
        [(c[0], c[1]) for c in CASES[:6]],
        ids=[
            "address reuse",
            "classic peel",
            "round payment",
            "script mismatch",
            "fresh change, larger",
            "reuse beats size",
        ],
    )
    def test_the_cases_the_heuristics_are_designed_for(self, outputs, truth):
        assert decide(outputs).index == truth
