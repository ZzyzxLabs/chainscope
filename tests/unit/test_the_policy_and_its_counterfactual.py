"""Ordered rules, a version on them, and what would have changed the answer.

Weights were the alternative and are worse in the one setting that matters. "The
weighted sum crossed 0.78" invites the question of where 0.78 came from, and the
honest answer is usually that somebody tuned it until the alert volume looked
reasonable. "Rule `sanctions-direct` fired, and here is the sentence explaining
why it exists" is an answer.

The counterfactual is the part no closed vendor offers, because offering it
means opening the scoring: *"this is a hold because of one OFAC tag on an
address two hops away, and without it the answer is allow"*. That can be argued
with, taken to the customer, or acted on.
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
    Exposure,
    Policy,
    PolicyError,
    Rule,
    Screen,
    Signal,
    StopReason,
    When,
)

ETH = ChainId.evm(1)
NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)
USDC = Amount(1_000_000, 6, "USDC")
ADDRESS = "0x" + "5d" * 20


def claim(source: str, category: Category, confidence: Confidence) -> Attribution:
    return Attribution(
        address="0x" + "8" * 40,
        chain=ETH,
        label="Something",
        category=category,
        confidence=confidence,
        method=Method.LIST,
        source=source,
        observed_at=NOW,
        rationale="recorded for the test",
    )


def exposure(
    category: Category = Category.SANCTIONED,
    hops: int = 0,
    share: str = "0.31",
    sources: tuple[str, ...] = ("OFAC SDN list",),
    confidence: Confidence = Confidence.CERTAIN,
    stopped_at: StopReason = StopReason.EXHAUSTED,
) -> Exposure:
    return Exposure(
        source=Entity(key="e", name="Some Entity"),
        category=category,
        amount=Amount(310_000, 6, "USDC"),
        share=Decimal(share),
        hops=hops,
        path=tuple(f"0x{i:040x}" for i in range(hops)),
        evidence=tuple(claim(s, category, confidence) for s in sources),
        stopped_at=stopped_at,
    )


def screen(**kw: object) -> Screen:
    base: dict[str, object] = {
        "address": ADDRESS,
        "amount": USDC,
        "at": NOW,
        "taint": "fifo",
    }
    base.update(kw)
    return Screen(**base)  # type: ignore[arg-type]


SANCTIONS_DIRECT = Rule(
    id="sanctions-direct",
    when=When(categories=frozenset({Category.SANCTIONED}), max_hops=0),
    then=Action.REJECT,
    because="OFAC has published no de minimis exposure level.",
)
MIXER_NEAR = Rule(
    id="mixer-near",
    when=When(categories=frozenset({Category.MIXER}), max_hops=2),
    then=Action.ENHANCED_KYC,
    because="Mixer proximity is not proof of wrongdoing but does need a customer explanation.",
)
FRESH_SHAPE = Rule(
    id="fresh-shape",
    when=When(signals=frozenset({"probing"})),
    then=Action.HOLD,
    because="A test payment before the full amount is the shape of a staged payout.",
)


def policy(*rules: Rule, default: Action = Action.ALLOW) -> Policy:
    return Policy(name="acme-deposits", version=4, rules=rules, default=default)


# ------------------------------------------------------------ ordered rules


def test_the_first_matching_rule_wins() -> None:
    both = policy(SANCTIONS_DIRECT, MIXER_NEAR)
    found = both.match(screen(exposures=(exposure(), exposure(Category.MIXER, hops=1))))
    assert found is not None
    assert found.rule.id == "sanctions-direct"


def test_reordering_changes_the_answer_which_is_why_the_version_matters() -> None:
    subject = screen(exposures=(exposure(), exposure(Category.MIXER, hops=1)))
    assert policy(SANCTIONS_DIRECT, MIXER_NEAR).choose(subject)[0] is Action.REJECT
    assert policy(MIXER_NEAR, SANCTIONS_DIRECT).choose(subject)[0] is Action.ENHANCED_KYC


def test_the_decision_carries_the_rule_and_the_policy_version() -> None:
    made = policy(SANCTIONS_DIRECT).decide(screen(exposures=(exposure(),)), at=NOW)
    assert made.rule_id == "sanctions-direct"
    assert made.policy_version == 4
    assert "de minimis" in made.because


def test_the_default_is_hold_unless_written_down() -> None:
    """A rule set that has not considered a case has not cleared it."""
    assert Policy(name="p", version=1).default is Action.HOLD


def test_two_rules_cannot_share_an_id() -> None:
    """A decision records which rule fired; an ambiguous id makes it useless."""
    with pytest.raises(PolicyError, match="share the id"):
        Policy(name="p", version=1, rules=(SANCTIONS_DIRECT, SANCTIONS_DIRECT))


def test_a_rule_needs_a_justification() -> None:
    with pytest.raises(PolicyError, match="justification"):
        Rule(id="r", when=When(), then=Action.HOLD, because="  ")


def test_versions_start_at_one() -> None:
    with pytest.raises(PolicyError, match="versions start at 1"):
        Policy(name="p", version=0)


# ---------------------------------------------------------------- matching


def test_hops_are_inclusive_and_bound_the_rule() -> None:
    near = policy(MIXER_NEAR, default=Action.ALLOW)
    assert near.choose(screen(exposures=(exposure(Category.MIXER, hops=2),)))[0] is (
        Action.ENHANCED_KYC
    )
    assert near.choose(screen(exposures=(exposure(Category.MIXER, hops=3),)))[0] is (
        Action.ALLOW
    )


def test_a_rule_can_require_well_sourced_evidence() -> None:
    """How an institution says 'act on OFAC, review heuristics'."""
    strict = policy(
        Rule(
            id="strong-only",
            when=When(
                categories=frozenset({Category.SANCTIONED}),
                min_confidence=Confidence.HIGH,
            ),
            then=Action.REJECT,
            because="Only well-sourced sanctions claims justify refusing funds.",
        ),
        default=Action.ALLOW,
    )
    weak = screen(exposures=(exposure(confidence=Confidence.LOW),))
    assert strict.choose(weak)[0] is Action.ALLOW
    strong = screen(exposures=(exposure(confidence=Confidence.CERTAIN),))
    assert strict.choose(strong)[0] is Action.REJECT


def test_a_signal_rule_does_not_fire_on_attributed_exposure() -> None:
    """A rule naming only signals must not match a screen merely because it has
    exposures."""
    shape = policy(FRESH_SHAPE, default=Action.ALLOW)
    assert shape.choose(screen(exposures=(exposure(),)))[0] is Action.ALLOW


def test_a_signal_rule_fires_on_shape() -> None:
    shape = policy(FRESH_SHAPE, default=Action.ALLOW)
    subject = screen(signals=(Signal(name="probing", summary="test payment first"),))
    assert shape.choose(subject)[0] is Action.HOLD


# ------------------------------------------------------------------- floors


def test_allow_is_floored_to_hold_on_an_incomplete_screen() -> None:
    """And says so --- a floor that adjusted silently would be the same defect
    as a threshold nobody wrote down."""
    action, _rule, _because, notes = policy(default=Action.ALLOW).choose(
        screen(unreachable_sources=("eth_labels: Ethereum only",))
    )
    assert action is Action.HOLD
    assert notes and "reduced to `hold`" in notes[0]
    assert "eth_labels" in notes[0]


def test_a_trace_stopping_at_a_service_also_floors_allow() -> None:
    stopped = exposure(category=Category.CEX, hops=2, stopped_at=StopReason.SERVICE)
    action, *_ = policy(default=Action.ALLOW).choose(screen(exposures=(stopped,)))
    assert action is Action.HOLD


def test_an_irreversible_action_on_shape_alone_is_floored_to_escalate() -> None:
    reject_on_shape = policy(
        Rule(
            id="shape-reject",
            when=When(signals=frozenset({"probing"})),
            then=Action.REJECT,
            because="Written badly on purpose, to prove the floor holds.",
        )
    )
    subject = screen(signals=(Signal(name="probing", summary="test payment first"),))
    action, _rule, _because, notes = reject_on_shape.choose(subject)
    assert action is Action.ESCALATE
    assert notes and "cannot justify" in notes[0]


def test_the_floors_only_ever_make_the_answer_more_conservative() -> None:
    made = policy(default=Action.ALLOW).decide(
        screen(unreachable_sources=("everything failed",)), at=NOW
    )
    assert made.action is Action.HOLD
    assert made.notes


# ---------------------------------------------------------- counterfactual


def test_the_counterfactual_names_the_load_bearing_source() -> None:
    made = policy(SANCTIONS_DIRECT, default=Action.ALLOW).decide(
        screen(exposures=(exposure(),)), at=NOW
    )
    assert made.action is Action.REJECT
    (what_if,) = made.counterfactuals
    assert what_if.without == "OFAC SDN list"
    assert what_if.then is Action.ALLOW


def test_a_corroborated_exposure_does_not_vanish_when_one_source_is_removed() -> None:
    """Reporting that it would overstates how much rests on any single tag,
    which is the opposite of what the counterfactual is for."""
    made = policy(SANCTIONS_DIRECT, default=Action.ALLOW).decide(
        screen(exposures=(exposure(sources=("OFAC SDN list", "internal review")),)),
        at=NOW,
    )
    assert made.action is Action.REJECT
    assert made.counterfactuals == ()


def test_removals_that_change_nothing_are_not_reported() -> None:
    """A list of things that made no difference is noise; the reader is looking
    for the load-bearing one.

    The rule and the default agree here, so removing the evidence changes
    nothing. Written with `HOLD` rather than `REJECT` deliberately: a REJECT
    default would be floored to `escalate` once the last exposure was removed,
    and that *is* a change --- the first draft of this test asserted otherwise
    and the floor was right.
    """
    hold_either_way = policy(
        Rule(
            id="hold-sanctions",
            when=When(categories=frozenset({Category.SANCTIONED})),
            then=Action.HOLD,
            because="Held pending review whatever else is true.",
        ),
        default=Action.HOLD,
    )
    made = hold_either_way.decide(screen(exposures=(exposure(),)), at=NOW)
    assert made.action is Action.HOLD
    assert made.counterfactuals == ()


def test_a_signal_can_be_the_load_bearing_thing() -> None:
    made = policy(FRESH_SHAPE, default=Action.ALLOW).decide(
        screen(signals=(Signal(name="probing", summary="test payment first"),)), at=NOW
    )
    assert made.action is Action.HOLD
    (what_if,) = made.counterfactuals
    assert "probing" in what_if.without
    assert what_if.then is Action.ALLOW


def test_the_counterfactual_list_is_stable_between_runs() -> None:
    """Two screenings of one deposit have to produce the same record, or the
    record is not reproducible."""
    subject = screen(
        exposures=(
            exposure(sources=("OFAC SDN list",)),
            exposure(Category.MIXER, hops=1, sources=("internal review",)),
        )
    )
    made = policy(SANCTIONS_DIRECT, MIXER_NEAR, default=Action.ALLOW)
    first = made.decide(subject, at=NOW).counterfactuals
    second = made.decide(subject, at=NOW).counterfactuals
    assert [c.without for c in first] == [c.without for c in second]


def test_the_counterfactual_can_be_switched_off() -> None:
    made = policy(SANCTIONS_DIRECT).decide(
        screen(exposures=(exposure(),)), at=NOW, counterfactuals=False
    )
    assert made.counterfactuals == ()
