"""The three taint models disagree, and the disagreement is the finding.

A vendor picks one and reports a number, which then gets defended as though the
model were a fact about the money rather than a convention about bookkeeping.
It is not: on 100 ETH stolen, this codebase measures FIFO attributing 100.0,
haircut 86.4 (13.6 lost to rounding, unrecoverable) and poison 320.0 (220
manufactured across five times the addresses).

What matters for a decision is not which number is right. It is whether the
answer *changes* with the choice:

* all three agree -> the conclusion survives a challenge to the methodology;
* they disagree about the amount -> exposure is real, its size is conventional;
* they disagree about whether there is *any* -> the decision is an artefact of
  the bookkeeping rule, and whoever signs it is entitled to know that.

`contested` is the third case and is deliberately not the same as a wide
spread.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from chainscope.analysis.taint import TaintPolicy
from chainscope.risk import compare_taint_models

THIEF = "0x" + "11" * 20
MIXER = "0x" + "22" * 20
VICTIM_OF_NOTHING = "0x" + "33" * 20
CLEAN = "0x" + "44" * 20
OUT = "0x" + "55" * 20

ETH = 10**18


def transfer(sender: str, recipient: str, amount: int, block: int) -> Any:
    return SimpleNamespace(
        sender=SimpleNamespace(raw=sender, key=sender.lower()),
        recipient=SimpleNamespace(raw=recipient, key=recipient.lower()),
        amount=SimpleNamespace(raw=amount),
        block=block,
        index=0,
        asset=None,
        tx=SimpleNamespace(hash=f"0x{block:064x}"),
    )


def test_all_three_run_and_are_reported_separately() -> None:
    rows = [transfer(THIEF, OUT, 100 * ETH, 1)]
    found = compare_taint_models(rows, {THIEF: 100 * ETH}, OUT, 100 * ETH)
    assert {o.policy for o in found.outcomes} == {
        TaintPolicy.FIFO,
        TaintPolicy.HAIRCUT,
        TaintPolicy.POISON,
    }


def test_a_direct_transfer_is_unanimous() -> None:
    """No mixing, so there is nothing for the conventions to differ about ---
    and saying so is the case where a methodology challenge changes nothing."""
    rows = [transfer(THIEF, OUT, 100 * ETH, 1)]
    found = compare_taint_models(rows, {THIEF: 100 * ETH}, OUT, 100 * ETH)
    assert found.unanimous_tainted
    assert not found.contested
    assert found.spread == Decimal(0)
    assert "does not depend on which rule" in found.explain()


def test_poison_finding_nothing_is_the_strongest_clean_result() -> None:
    """Poison is the most generous painter there is. Poison finding nothing
    means none ever passed through, not merely that none is held now."""
    rows = [transfer(CLEAN, OUT, 5 * ETH, 1)]
    found = compare_taint_models(
        rows, {THIEF: 100 * ETH}, OUT, 5 * ETH, opening_balances={CLEAN: 5 * ETH}
    )
    assert found.unanimous_clean
    assert not found.contested
    assert "including poison" in found.explain()


def test_mixing_makes_the_models_diverge() -> None:
    """Stolen and clean value land in one address, which then pays out less
    than it holds. FIFO says the first value in left first; haircut splits
    proportionally; poison taints the lot. This is the whole disagreement."""
    rows = [
        transfer(THIEF, MIXER, 20 * ETH, 1),
        transfer(CLEAN, MIXER, 80 * ETH, 2),
        transfer(MIXER, OUT, 20 * ETH, 3),
    ]
    found = compare_taint_models(
        rows,
        {THIEF: 20 * ETH},
        OUT,
        20 * ETH,
        opening_balances={CLEAN: 80 * ETH},
    )
    shares = {o.policy: o.share for o in found.outcomes}
    # FIFO: the thief's 20 arrived first, so the 20 that left is exactly it.
    assert shares[TaintPolicy.FIFO] == Decimal(1)
    # Haircut: the mixer held 20% taint, so a fifth of the payout carries it.
    assert Decimal("0.15") < shares[TaintPolicy.HAIRCUT] < Decimal("0.25")
    assert found.spread > Decimal("0.5")
    assert found.unanimous_tainted


def test_the_explanation_names_the_disagreement_for_the_approver() -> None:
    rows = [
        transfer(THIEF, MIXER, 20 * ETH, 1),
        transfer(CLEAN, MIXER, 80 * ETH, 2),
        transfer(MIXER, OUT, 20 * ETH, 3),
    ]
    found = compare_taint_models(
        rows,
        {THIEF: 20 * ETH},
        OUT,
        20 * ETH,
        opening_balances={CLEAN: 80 * ETH},
    )
    said = found.explain()
    assert "fifo" in said and "haircut" in said and "poison" in said


def test_contested_is_not_the_same_as_a_wide_spread() -> None:
    """Two models at 4% and 40% agree the address is exposed and differ on
    size. One at 0% and another at 40% disagree about the fact, and only the
    second makes the decision an artefact of the rule."""
    from chainscope.risk.agreement import Agreement, ModelOutcome

    def outcome(policy: TaintPolicy, share: str, tainted: int) -> ModelOutcome:
        return ModelOutcome(
            policy=policy, tainted=tainted, share=Decimal(share), touched=1, unresolved=0
        )

    wide = Agreement(
        address=OUT,
        balance=ETH,
        outcomes=(
            outcome(TaintPolicy.FIFO, "0.04", 1),
            outcome(TaintPolicy.HAIRCUT, "0.40", 1),
        ),
    )
    assert wide.spread > Decimal("0.3")
    assert not wide.contested

    split = Agreement(
        address=OUT,
        balance=ETH,
        outcomes=(
            outcome(TaintPolicy.FIFO, "0", 0),
            outcome(TaintPolicy.HAIRCUT, "0.40", 1),
        ),
    )
    assert split.contested
    assert "rests on" in split.explain()


def test_unresolved_transfers_are_reported_per_model() -> None:
    """A model that could not apply the history has not produced a cautious
    answer, it has produced an uninformed one."""
    # No opening balance, so the mixer's first spend has nothing to draw on.
    rows = [transfer(MIXER, OUT, 50 * ETH, 1)]
    found = compare_taint_models(rows, {THIEF: 10 * ETH}, OUT, 50 * ETH)
    assert any(o.unresolved for o in found.outcomes)


def test_a_negative_balance_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        compare_taint_models([], {THIEF: 1}, OUT, -1)


def test_a_zero_balance_does_not_divide_by_zero() -> None:
    found = compare_taint_models([], {THIEF: 1}, VICTIM_OF_NOTHING, 0)
    assert all(o.share == Decimal(0) for o in found.outcomes)
