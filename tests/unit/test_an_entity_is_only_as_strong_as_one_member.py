"""The entity model, held to the four rules it exists to enforce.

Each of these has a tempting simpler version, and each simpler version produces
a wrongly frozen account rather than a wrong number. That is why they are
structural here instead of being conventions somebody remembers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ChainId
from chainscope.core.entity import (
    Entity,
    EntityError,
    Function,
    Incident,
    Membership,
    Role,
    RoleKind,
    from_attributions,
)

ETH = ChainId.evm(1)
WHEN = datetime(2026, 8, 2, tzinfo=timezone.utc)


def member(
    address: str,
    confidence: Confidence,
    *,
    function: Function = Function.UNKNOWN,
    source: str = "exchange's published list",
) -> Membership:
    return Membership(
        address=address,
        chain=ETH,
        confidence=confidence,
        source=source,
        function=function,
    )


# ------------------------------------------------------- the weakest link


def test_the_entity_is_as_strong_as_its_worst_member() -> None:
    """Nine certainties and one guess is not 'mostly certain'."""
    entity = Entity(
        key="acme",
        name="Acme Exchange",
        members=(
            *(member(f"0x{i:040x}", Confidence.CERTAIN) for i in range(9)),
            member("0x" + "aa" * 20, Confidence.SPECULATIVE, source="co-spend heuristic"),
        ),
    )
    assert entity.floor is Confidence.SPECULATIVE


def test_a_guess_does_not_borrow_the_confidence_of_the_published_ones() -> None:
    """The whole reason membership carries its own evidence."""
    published = "0x" + "11" * 20
    guessed = "0x" + "22" * 20
    entity = Entity(
        key="acme",
        name="Acme Exchange",
        members=(
            member(published, Confidence.CERTAIN),
            member(guessed, Confidence.SPECULATIVE, source="co-spend heuristic"),
        ),
    )
    assert entity.confidence_for(published) is Confidence.CERTAIN
    assert entity.confidence_for(guessed) is Confidence.SPECULATIVE


def test_an_address_that_is_not_a_member_answers_none_not_a_default() -> None:
    """A default here would make every unknown address a weak member."""
    entity = Entity(
        key="acme", name="Acme", members=(member("0x" + "11" * 20, Confidence.HIGH),)
    )
    assert entity.confidence_for("0x" + "99" * 20) is None


def test_membership_needs_a_source() -> None:
    with pytest.raises(EntityError, match="source"):
        Membership(address="0x" + "11" * 20, chain=ETH, confidence=Confidence.HIGH, source="  ")


def test_the_same_address_cannot_be_added_twice_with_two_reasons() -> None:
    """Keeping one reason silently discards the evidence for the other."""
    address = "0x" + "11" * 20
    with pytest.raises(EntityError, match="twice"):
        Entity(
            key="acme",
            name="Acme",
            members=(
                member(address, Confidence.CERTAIN),
                member(address, Confidence.LOW, source="a guess"),
            ),
        )


def test_replacing_a_membership_is_refused_rather_than_silent() -> None:
    address = "0x" + "11" * 20
    entity = Entity(key="acme", name="Acme", members=(member(address, Confidence.CERTAIN),))
    with pytest.raises(EntityError, match="already in"):
        entity.with_member(member(address, Confidence.LOW, source="a guess"))


# ------------------------------------------------------------------- roles


def test_naming_an_attacker_needs_a_source() -> None:
    with pytest.raises(EntityError, match="source"):
        Role(incident="lpdfi", kind=RoleKind.ATTACKER, confidence=Confidence.HIGH, source="")


def test_laundering_is_a_claim_about_knowledge_and_needs_evidence_for_it() -> None:
    """RECIPIENT says what was seen. LAUNDERER says what was intended."""
    with pytest.raises(EntityError, match="RECIPIENT"):
        Role(
            incident="lpdfi",
            kind=RoleKind.LAUNDERER,
            confidence=Confidence.MEDIUM,
            source="analyst",
        )
    # The observable version is always available.
    Role(
        incident="lpdfi",
        kind=RoleKind.RECIPIENT,
        confidence=Confidence.MEDIUM,
        source="analyst",
    )


def test_an_entity_cannot_be_both_sides_of_one_incident() -> None:
    entity = Entity(key="e", name="E").with_role(
        Role(incident="lpdfi", kind=RoleKind.VICTIM, confidence=Confidence.HIGH, source="s")
    )
    with pytest.raises(EntityError, match="already has a role"):
        entity.with_role(
            Role(
                incident="lpdfi",
                kind=RoleKind.ATTACKER,
                confidence=Confidence.HIGH,
                source="s",
            )
        )


def test_a_victim_of_one_incident_can_be_the_attacker_in_another() -> None:
    """Roles are per incident, so this must be representable."""
    entity = (
        Entity(key="e", name="E")
        .with_role(
            Role(incident="a", kind=RoleKind.VICTIM, confidence=Confidence.HIGH, source="s")
        )
        .with_role(
            Role(incident="b", kind=RoleKind.ATTACKER, confidence=Confidence.HIGH, source="s")
        )
    )
    assert entity.role_in("a") is not None
    assert entity.role_in("a").kind is RoleKind.VICTIM  # type: ignore[union-attr]
    assert entity.role_in("b").kind is RoleKind.ATTACKER  # type: ignore[union-attr]


def test_there_is_no_way_to_ask_whether_an_entity_is_bad() -> None:
    """Every question about wrongdoing has to name an incident. The one
    aggregate that exists is named so that reaching for it is a decision."""
    assert not hasattr(Entity(key="e", name="E"), "is_bad")
    assert not hasattr(Entity(key="e", name="E"), "risk_score")
    assert hasattr(Entity(key="e", name="E"), "is_attacker_anywhere")


# ------------------------------------------------------- control changing


def test_a_compromised_exchange_is_a_different_counterparty_before_and_after() -> None:
    seized = Entity(
        key="acme",
        name="Acme Exchange (under attacker control)",
        controlled_since=WHEN,
    )
    assert seized.controlled_at(WHEN)
    assert seized.controlled_at(WHEN + timedelta(days=1))
    assert not seized.controlled_at(WHEN - timedelta(seconds=1))


def test_an_entity_with_no_recorded_change_answers_yes_for_any_time() -> None:
    assert Entity(key="e", name="E").controlled_at(WHEN)


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(EntityError, match="timezone-aware"):
        Entity(key="e", name="E", controlled_since=datetime(2026, 8, 2))
    with pytest.raises(EntityError, match="timezone-aware"):
        Entity(key="e", name="E").controlled_at(datetime(2026, 8, 2))


# --------------------------------------------------------------- functions


def test_deposit_addresses_are_reachable_as_a_group() -> None:
    """Traversal branches on this: value into a hot wallet identifies nobody,
    value into a deposit address identifies one customer."""
    entity = Entity(
        key="acme",
        name="Acme",
        members=(
            member("0x" + "11" * 20, Confidence.HIGH, function=Function.HOT_WALLET),
            member("0x" + "22" * 20, Confidence.HIGH, function=Function.DEPOSIT),
            member("0x" + "33" * 20, Confidence.HIGH, function=Function.DEPOSIT),
        ),
    )
    assert len(entity.functions(Function.DEPOSIT)) == 2
    assert len(entity.functions(Function.HOT_WALLET)) == 1


# ------------------------------------------------------ built from claims


def test_building_from_attributions_keeps_every_source() -> None:
    claims = [
        Attribution(
            address="0x" + "11" * 20,
            chain=ETH,
            label="Acme",
            category=Category.CEX,
            confidence=Confidence.CERTAIN,
            method=Method.LABEL,
            source="published list",
        ),
        Attribution(
            address="0x" + "22" * 20,
            chain=ETH,
            label="Acme",
            category=Category.CEX,
            confidence=Confidence.LOW,
            method=Method.INFERENCE,
            source="co-spend",
            # `Attribution` already refuses a weak claim with no reasoning.
            rationale="spent together with a known Acme wallet in one tx",
        ),
    ]
    entity = from_attributions("acme", "Acme", claims, category=Category.CEX)
    assert entity.confidence_for("0x" + "11" * 20) is Confidence.CERTAIN
    assert entity.confidence_for("0x" + "22" * 20) is Confidence.LOW
    assert {m.source for m in entity.members} == {"published list", "co-spend"}
    assert entity.floor is Confidence.LOW


def test_a_claim_with_no_chain_is_refused() -> None:
    """Case folding is chain-specific and getting it wrong silently merges or
    splits addresses."""
    claim = Attribution(
        address="0x" + "11" * 20,
        chain=None,
        label="Acme",
        category=Category.CEX,
        confidence=Confidence.HIGH,
        method=Method.LABEL,
        source="list",
    )
    with pytest.raises(EntityError, match="no chain"):
        from_attributions("acme", "Acme", [claim])


def test_an_incident_needs_a_key_and_an_aware_timestamp() -> None:
    with pytest.raises(EntityError, match="key"):
        Incident(key=" ", name="LpdFi")
    with pytest.raises(EntityError, match="timezone-aware"):
        Incident(key="lpdfi", name="LpdFi", occurred_at=datetime(2026, 8, 2))
