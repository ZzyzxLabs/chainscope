"""Deposit screening: the refusals that make a decision defensible.

Each of these is a shortcut the field has taken and been penalised for. They
are enforced in the type rather than in a convention because the convention is
what erodes when somebody is shipping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ChainId
from chainscope.core.entity import Entity, RoleKind
from chainscope.core.units import Amount
from chainscope.risk import Directness, Exposure, ExposureError, Screen, StopReason

ETH = ChainId.evm(1)
AT = datetime(2026, 8, 2, tzinfo=timezone.utc)
USDC = Amount(1_000_000, 6, "USDC")


def evidence(category: Category = Category.SANCTIONED) -> tuple[Attribution, ...]:
    return (
        Attribution(
            address="0x" + "8" * 40,
            chain=ETH,
            label="Tornado Cash",
            category=category,
            confidence=Confidence.CERTAIN,
            method=Method.LIST,
            source="OFAC SDN list",
            observed_at=AT,
        ),
    )


def exposure(**kw: object) -> Exposure:
    base: dict[str, object] = {
        "source": Entity(key="tc", name="Tornado Cash"),
        "category": Category.SANCTIONED,
        "amount": Amount(310_000, 6, "USDC"),
        "share": Decimal("0.31"),
        "hops": 2,
        "path": ("0x" + "a" * 40, "0x" + "b" * 40),
        "evidence": evidence(),
    }
    base.update(kw)
    return Exposure(**base)  # type: ignore[arg-type]


# ------------------------------------------------------- evidence is required


def test_an_exposure_without_evidence_is_refused() -> None:
    """'Which source said so' is the first question asked of any decision."""
    with pytest.raises(ExposureError, match="assertion"):
        exposure(evidence=())


def test_a_role_without_an_incident_is_refused() -> None:
    """'Attacker' is a claim about a person. 'Attacker in incident X' is a
    claim about an event, and only the second is defensible."""
    with pytest.raises(ExposureError, match="incident"):
        exposure(role=RoleKind.ATTACKER, incident="")
    exposure(role=RoleKind.ATTACKER, incident="lpdfi-2026-08")


# --------------------------------------------------- direct is not indirect


def test_directness_is_derived_so_it_cannot_disagree_with_hops() -> None:
    assert exposure(hops=0, path=()).directness is Directness.DIRECT
    assert exposure(hops=1, path=("0x" + "a" * 40,)).directness is Directness.INDIRECT


def test_a_direct_exposure_cannot_carry_an_intermediate_path() -> None:
    with pytest.raises(ExposureError, match="no intermediate path"):
        exposure(hops=0, path=("0x" + "a" * 40,))


def test_hops_are_kept_per_item_not_flattened() -> None:
    """No regulator has published a hop threshold, so the tool must not pick
    one by merging the distances away."""
    screen = Screen(
        address="0x" + "1" * 40,
        amount=USDC,
        at=AT,
        taint="fifo",
        exposures=(
            exposure(hops=0, path=(), share=Decimal("0.1")),
            exposure(hops=4, share=Decimal("0.2")),
        ),
    )
    assert len(screen.within(0)) == 1
    assert len(screen.within(4)) == 2


# ------------------------------------------------- stopping is not concluding


def test_stopping_at_a_service_is_not_a_conclusion() -> None:
    """Exchanges pool unrelated customers through one address, so a trace that
    runs onward through a hot wallet is an artefact, not a flow."""
    assert not exposure(stopped_at=StopReason.SERVICE).is_conclusive
    assert not exposure(stopped_at=StopReason.HOP_LIMIT).is_conclusive
    assert not exposure(stopped_at=StopReason.UNREACHABLE).is_conclusive
    assert exposure(stopped_at=StopReason.EXHAUSTED).is_conclusive


def test_a_screen_is_incomplete_when_a_trace_stopped_early() -> None:
    screen = Screen(
        address="0x" + "1" * 40,
        amount=USDC,
        at=AT,
        taint="fifo",
        exposures=(exposure(stopped_at=StopReason.SERVICE),),
    )
    assert not screen.complete


# ------------------------------------------------ silence is not a clean bill


def test_nothing_found_with_a_failed_source_is_not_clean() -> None:
    """The distinction most tools lose: 'no exposure' reads the same whether
    every source answered or none did."""
    screen = Screen(
        address="0x" + "1" * 40,
        amount=USDC,
        at=AT,
        taint="fifo",
        unreachable_sources=("eth_labels: covers Ethereum only",),
    )
    assert not screen.exposures
    assert not screen.complete
    assert not screen.clean


def test_nothing_found_with_every_source_answering_is_clean() -> None:
    screen = Screen(address="0x" + "1" * 40, amount=USDC, at=AT, taint="fifo")
    assert screen.clean


# ----------------------------------------------------- there is no score


def test_there_is_no_risk_score_anywhere() -> None:
    """The type describes; the policy decides. A scalar here would be a policy
    judgement smuggled into the input."""
    screen = Screen(address="0x" + "1" * 40, amount=USDC, at=AT, taint="fifo")
    for banned in ("risk_score", "score", "severity", "verdict", "decision"):
        assert not hasattr(screen, banned), banned
        assert not hasattr(exposure(), banned), banned


def test_shares_cannot_report_more_than_the_whole_deposit() -> None:
    """Haircut attribution does not partition, so naive summing reports 140%
    of a deposit as tainted."""
    screen = Screen(
        address="0x" + "1" * 40,
        amount=USDC,
        at=AT,
        taint="haircut",
        exposures=(
            exposure(share=Decimal("0.8")),
            exposure(share=Decimal("0.6")),
        ),
    )
    assert screen.total_share() == Decimal(1)


def test_a_share_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ExposureError, match=r"\[0, 1\]"):
        exposure(share=Decimal("1.5"))


def test_negative_hops_are_refused() -> None:
    with pytest.raises(ExposureError, match="negative"):
        exposure(hops=-1)


# -------------------------------------------------- the taint model is named


def test_the_screen_records_which_taint_model_produced_it() -> None:
    """FIFO, haircut and poison disagree, so a result that does not say which
    ran cannot be reproduced."""
    screen = Screen(address="0x" + "1" * 40, amount=USDC, at=AT, taint="fifo")
    assert screen.taint == "fifo"


def test_the_screen_is_asked_about_a_time_not_about_now() -> None:
    """A sanction listed after the deposit is not exposure at the time of the
    deposit. Whether it is still reportable is a policy question, and policy
    can only ask it if the time is carried."""
    screen = Screen(address="0x" + "1" * 40, amount=USDC, at=AT, taint="fifo")
    assert screen.at == AT


def test_filtering_by_category_keeps_the_evidence() -> None:
    screen = Screen(
        address="0x" + "1" * 40,
        amount=USDC,
        at=AT,
        taint="fifo",
        exposures=(
            exposure(category=Category.SANCTIONED),
            exposure(category=Category.MIXER, evidence=evidence(Category.MIXER)),
        ),
    )
    (found,) = screen.of(Category.MIXER)
    assert found.evidence[0].source == "OFAC SDN list"
