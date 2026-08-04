"""Screening at t=0, when no attribution exists yet.

The hardest case for this whole design, and the one it is worth being honest
about. When an exploit happens the attacker's address is unlabelled: nobody has
written the sentence a screen would match against, the analysts are still
reading the transaction, and a system built only on attribution answers "clean"
to the most dangerous deposit it will see all year — correctly, and uselessly.

What exists at t=0 is **shape**: an address created forty minutes ago, funded
once, forwarding everything, sending a small test payment before the real one.
`Signal` carries those, capped at MEDIUM.

What shape cannot do is justify an irreversible act. A fresh address forwarding
everything is also what somebody moving to a new wallet looks like, and the
costs are asymmetric — a hold is an inconvenience, a report is a permanent
record about a real person. So `reject` and `report` require attributed
evidence and `hold`, `enhanced_kyc` and `escalate` do not.

The honest summary of what this buys: it converts "we had no idea" into "we
held it for review", which is the difference on the day the money leaves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ChainId
from chainscope.core.entity import Entity
from chainscope.core.units import Amount
from chainscope.risk import (
    Action,
    Counterfactual,
    Decision,
    DecisionError,
    Exposure,
    ExposureError,
    Screen,
    Signal,
    StopReason,
)

ETH = ChainId.evm(1)
NOW = datetime(2026, 8, 2, 16, 0, tzinfo=timezone.utc)
USDC = Amount(1_000_000, 6, "USDC")
ADDRESS = "0x" + "5d" * 20


def signal(name: str = "probing", confidence: Confidence = Confidence.LOW) -> Signal:
    return Signal(
        name=name,
        summary="a 100 USDC test payment 77 blocks before the full amount",
        confidence=confidence,
        observed_at=NOW,
    )


def attributed() -> Exposure:
    return Exposure(
        source=Entity(key="tc", name="Tornado Cash"),
        category=Category.SANCTIONED,
        amount=Amount(310_000, 6, "USDC"),
        share=Decimal("0.31"),
        hops=1,
        path=("0x" + "ab" * 20,),
        evidence=(
            Attribution(
                address="0x" + "8" * 40,
                chain=ETH,
                label="Tornado Cash",
                category=Category.SANCTIONED,
                confidence=Confidence.CERTAIN,
                method=Method.LIST,
                source="OFAC SDN list",
                observed_at=NOW,
            ),
        ),
    )


def decide(action: Action, screen: Screen, **kw: object) -> Decision:
    base: dict[str, object] = {
        "action": action,
        "screen": screen,
        "policy_name": "acme-deposits",
        "policy_version": 4,
        "rule_id": "r1",
        "because": "test",
        "decided_at": NOW,
    }
    base.update(kw)
    return Decision(**base)  # type: ignore[arg-type]


def screen(**kw: object) -> Screen:
    base: dict[str, object] = {
        "address": ADDRESS,
        "amount": USDC,
        "at": NOW,
        "taint": "fifo",
    }
    base.update(kw)
    return Screen(**base)  # type: ignore[arg-type]


# ------------------------------------------------- shape is capped at MEDIUM


def test_a_behavioural_signal_cannot_claim_more_than_medium() -> None:
    """The same cap `Hypothesis` carries, for the same reason: a shape is
    consistent with innocent explanations."""
    with pytest.raises(ExposureError, match="MEDIUM"):
        signal(confidence=Confidence.HIGH)
    with pytest.raises(ExposureError, match="MEDIUM"):
        signal(confidence=Confidence.CERTAIN)
    signal(confidence=Confidence.MEDIUM)


def test_a_signal_needs_a_name_and_a_summary() -> None:
    with pytest.raises(ExposureError, match="name and a summary"):
        Signal(name="", summary="something happened")


# ------------------------------------ shape alone cannot do irreversible things


@pytest.mark.parametrize("action", [Action.REJECT, Action.REPORT])
def test_an_irreversible_action_cannot_rest_on_shape_alone(action: Action) -> None:
    """Rejecting returns money to an address that may not be theirs; reporting
    puts a name in front of an authority. Neither is undone tomorrow."""
    with pytest.raises(DecisionError, match="behavioural signals"):
        decide(action, screen(signals=(signal(),)))


@pytest.mark.parametrize("action", [Action.HOLD, Action.ENHANCED_KYC, Action.ESCALATE])
def test_the_reversible_actions_are_available_on_shape_alone(action: Action) -> None:
    """This is the whole value at t=0 --- 'we had no idea' becomes 'we held
    it'."""
    made = decide(action, screen(signals=(signal(),)))
    assert made.action is action


def test_an_irreversible_action_is_fine_once_something_is_attributed() -> None:
    made = decide(Action.REJECT, screen(exposures=(attributed(),)))
    assert made.action is Action.REJECT


# --------------------------------------------- releasing funds needs completeness


def test_allow_is_refused_when_a_source_could_not_be_read() -> None:
    """An absent answer is not a clean one."""
    with pytest.raises(DecisionError, match="incomplete screen"):
        decide(Action.ALLOW, screen(unreachable_sources=("eth_labels: Ethereum only",)))


def test_allow_is_refused_when_a_trace_stopped_at_a_service() -> None:
    """The money came from somewhere before the exchange, through an omnibus
    account nobody can see into."""
    stopped = Exposure(
        source=Entity(key="x", name="X"),
        category=Category.CEX,
        amount=Amount(1, 6, "USDC"),
        share=Decimal("0.01"),
        hops=2,
        path=("0x" + "ab" * 20, "0x" + "cd" * 20),
        evidence=attributed().evidence,
        stopped_at=StopReason.SERVICE,
    )
    with pytest.raises(DecisionError, match="incomplete screen"):
        decide(Action.ALLOW, screen(exposures=(stopped,)))


def test_hold_is_always_available_however_little_is_known() -> None:
    made = decide(Action.HOLD, screen(unreachable_sources=("everything failed",)))
    assert made.action is Action.HOLD


def test_allow_is_fine_on_a_complete_empty_screen() -> None:
    made = decide(Action.ALLOW, screen())
    assert made.action is Action.ALLOW


# ------------------------------------------------------------ the record itself


def test_a_decision_needs_its_justification_not_just_a_rule_id() -> None:
    """A rule id alone sends the reader to a file that may since have changed."""
    with pytest.raises(DecisionError, match="justification"):
        decide(Action.HOLD, screen(signals=(signal(),)), because="   ")


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(DecisionError, match="timezone-aware"):
        decide(Action.HOLD, screen(signals=(signal(),)), decided_at=datetime(2026, 8, 2))


def test_defensible_requires_being_bound_to_the_data() -> None:
    """Not a claim the decision is correct --- a claim it can be reconstructed."""
    unbound = decide(Action.REJECT, screen(exposures=(attributed(),)))
    assert not unbound.is_defensible
    bound = decide(Action.REJECT, screen(exposures=(attributed(),)), attestation="sha256:abc")
    assert bound.is_defensible


def test_the_explanation_separates_attributed_from_shape() -> None:
    """A reader must be able to tell which of the two they are looking at."""
    made = decide(
        Action.HOLD,
        screen(signals=(signal(),)),
        because="fresh address forwarding everything",
    )
    said = made.explain()
    assert "none attributed" in said
    assert "shape only" in said
    assert "probing" in said


def test_the_explanation_names_the_counterfactual() -> None:
    made = decide(
        Action.REJECT,
        screen(exposures=(attributed(),)),
        counterfactuals=(
            Counterfactual(without="OFAC SDN list on 0x8888…", then=Action.ALLOW),
        ),
    )
    said = made.explain()
    assert "without" in said and "allow" in said


def test_a_rescore_references_the_decision_it_replaces() -> None:
    """A tag landing today changes a deposit accepted last week, and the
    earlier decision must stay readable --- 'what did you know at the time' is
    the question that gets asked."""
    made = decide(
        Action.REPORT, screen(exposures=(attributed(),)), supersedes="dec-2026-07-28-11"
    )
    assert made.supersedes == "dec-2026-07-28-11"


def test_a_screen_with_signals_is_not_clean() -> None:
    """Nothing attributed is not the same as nothing to look at."""
    fresh = screen(signals=(signal(),))
    assert not fresh.clean
    assert fresh.unattributed
