"""Which addresses are one party, and what that party did.

`ResolvedEntity` in :mod:`chainscope.core.attribution` answers "what do we know
about *this address*". This module answers the question above it: **which
addresses are the same party, and on what evidence**. Everything a customer
actually buys sits on that — an exposure figure computed per address rather
than per entity undercounts by exactly the amount the entity took care to
split.

Four modelling decisions, each of which exists because the obvious version
produces a wrongly frozen account.

**Membership carries its own evidence, and an entity is only as strong as the
address you are asking about.** An entity holding nine addresses from an
exchange's published list and one added by a co-spend heuristic is not
uniformly certain. `Entity.confidence_for` answers per address; `Entity.floor`
is the weakest link. Averaging them is how a guess becomes a fact, and the
average is what almost every representation of this reaches for first.

**Attacker, victim and intermediary are roles in an incident, not properties of
an address.** The same address is the victim of one theft and the attacker in
another, and a tag reading "involved in incident X" collapses the distinction
that decides whether a customer gets their money back or gets reported. So a
`Role` binds an entity to an incident with a direction, and nothing here lets
an address be "bad" in the abstract.

**A deposit address belongs to two parties at once.** It is the exchange's
infrastructure and one customer's endpoint. Treating it as purely the
exchange's makes every customer of that exchange look like the exchange;
treating it as purely the customer's attributes the exchange's whole flow to
one person. `Function` records which kind of address this is so that traversal
and exposure can branch on it.

**Control changes, and the addresses do not.** A compromised exchange keeps its
addresses. An entity whose control changed on a date is not the same
counterparty before and after, and a screening decision that ignores the date
is scoring the wrong party. `Entity.controlled_since` exists to be checked
against the timestamp of the flow being screened, not against now.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..chains import address_key
from .attribution import Attribution, Category, Confidence
from .chainid import ChainId

__all__ = [
    "Entity",
    "EntityError",
    "Function",
    "Incident",
    "Membership",
    "Role",
    "RoleKind",
]


class EntityError(ValueError):
    """An entity was asked to represent something it must not."""


class Function(str, Enum):
    """What an address *does* for the entity that controls it.

    Traversal and exposure branch on this, and the branches are opposite. Value
    arriving at a **hot wallet** says nothing about any individual: the wallet
    pays everyone. Value arriving at a **deposit address** identifies one
    customer, which is the entire reason exchange deposit addresses are worth
    finding.
    """

    UNKNOWN = "unknown"

    HOT_WALLET = "hot_wallet"
    """Pays many, receives from few. Attributes to the service, never to a
    person."""

    DEPOSIT = "deposit"
    """Issued to one customer, forwards inward. Attributes to *that customer*
    as well as to the service, which is why `Membership` allows an address to
    belong to more than one entity."""

    CONSOLIDATION = "consolidation"
    """Sweeps deposits toward treasury. Service infrastructure."""

    TREASURY = "treasury"
    COLD = "cold"

    CONTRACT = "contract"
    """Code, not a party. An entity may own it; it does not decide anything."""

    OPERATIONAL = "operational"
    """Fee payers, relayers, gas stations."""


class RoleKind(str, Enum):
    """How an entity stood in relation to an incident.

    The three that matter are separated because collapsing them is the single
    most expensive modelling error in this field: money leaving a theft touches
    the victim's address, the attacker's address, and whatever it passed
    through, and a system that marks all three "associated with theft" freezes
    the victim.
    """

    ATTACKER = "attacker"
    VICTIM = "victim"
    INTERMEDIARY = "intermediary"
    """Passed through without evident knowledge --- a DEX, a bridge, a
    counterparty who sold something. Exposure to this is not exposure to the
    attacker."""

    LAUNDERER = "launderer"
    """Handled the proceeds *with* evident knowledge. A claim about intent, and
    therefore one this codebase will not infer: it requires a source."""

    RECIPIENT = "recipient"
    """Received proceeds. Deliberately weaker than LAUNDERER and much weaker
    than ATTACKER, and the correct default when all that is observed is that
    money arrived."""


@dataclass(frozen=True, slots=True)
class Incident:
    """A named event that entities have roles in.

    Separate from the entities so that one exploit is one record. Tagging every
    address with a copy of the story produces fifty slightly different stories.
    """

    key: str
    name: str
    occurred_at: datetime | None = None
    chain: ChainId | None = None
    source: str = ""
    """Where the incident itself is documented. An incident nobody published is
    still an incident, but a decision resting on one needs to say so."""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise EntityError("an incident needs a key")
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise EntityError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Role:
    """One entity's part in one incident, with its evidence."""

    incident: str
    kind: RoleKind
    confidence: Confidence
    source: str
    observed_at: datetime | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise EntityError(
                f"a {self.kind.value} role needs a source. Naming somebody an "
                f"attacker without saying who says so is the accusation this "
                f"package refuses to let anyone make by accident"
            )
        if self.kind is RoleKind.LAUNDERER and self.confidence < Confidence.HIGH:
            # Intent, not observation. Everything below HIGH describing intent
            # is speculation wearing a role name, and RECIPIENT says the
            # observable part without the accusation.
            raise EntityError(
                "LAUNDERER asserts knowledge, so it needs HIGH confidence or "
                "better. Use RECIPIENT for 'the money arrived', which is what "
                "was actually seen"
            )


@dataclass(frozen=True, slots=True)
class Membership:
    """One address's place in one entity, and why it is believed to be there.

    The `why` is not decoration. When a frozen customer asks how their address
    came to be attached to a sanctioned exchange, this field is the answer, and
    a membership set that cannot answer it per address is not defensible.
    """

    address: str
    chain: ChainId
    confidence: Confidence
    source: str
    function: Function = Function.UNKNOWN
    observed_at: datetime | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise EntityError(f"membership of {self.address} needs a source")


@dataclass(frozen=True, slots=True)
class Entity:
    """A party, and the addresses believed to be theirs.

    Immutable. Adding an address produces a new entity, so a case can hold the
    view as it stood when a decision was made rather than as it stands now ---
    which is what "why did you freeze this in March" requires.
    """

    key: str
    name: str
    category: Category = Category.UNKNOWN
    members: tuple[Membership, ...] = ()
    roles: tuple[Role, ...] = ()
    controlled_since: datetime | None = None
    """When the current controller took over, if that is known to have changed.

    Checked against the *time of the flow being screened*, never against now.
    A compromised exchange keeps its addresses, and value that moved before the
    compromise moved to a different party than value that moved after."""

    note: str = ""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise EntityError("an entity needs a key")
        if self.controlled_since is not None and self.controlled_since.tzinfo is None:
            raise EntityError("controlled_since must be timezone-aware")
        seen: set[tuple[str, str]] = set()
        for member in self.members:
            # `address_key`, never `.lower()`. Folding case is right for EVM and
            # destroys a base58 or bech32 address, and this type carries a chain
            # on every membership precisely so the right rule is reachable.
            identity = (str(member.chain), address_key(member.chain, member.address))
            if identity in seen:
                raise EntityError(
                    f"{member.address} appears twice in {self.key}. Two "
                    f"memberships for one address means two different reasons "
                    f"for believing it, and keeping only one of them loses the "
                    f"evidence for the other"
                )
            seen.add(identity)

    # ------------------------------------------------------------------ shape

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(m.address for m in self.members)

    def member_for(self, address: str, chain: ChainId | None = None) -> Membership | None:
        for member in self.members:
            if chain is not None and member.chain != chain:
                continue
            # Folded under the *member's* chain, which is the only chain that
            # can be correct for the comparison --- the caller may not have
            # passed one.
            if address_key(member.chain, address) == address_key(member.chain, member.address):
                return member
        return None

    def confidence_for(self, address: str, chain: ChainId | None = None) -> Confidence | None:
        """How strongly *this address* belongs, not how strong the entity is.

        The distinction the whole module exists for. An exchange entity built
        from a published address list plus one heuristic guess will answer
        CERTAIN for the published ones and SPECULATIVE for the guess, and a
        decision about the guess must not borrow the confidence of the others.
        """
        member = self.member_for(address, chain)
        return member.confidence if member else None

    @property
    def floor(self) -> Confidence:
        """The weakest membership. What the entity as a whole can bear.

        Not the mean and not the mode. A claim about "this entity" is a claim
        about every address in it, and it is true only as far as the worst one.
        """
        if not self.members:
            return Confidence.SPECULATIVE
        return min(m.confidence for m in self.members)

    def functions(self, function: Function) -> tuple[Membership, ...]:
        return tuple(m for m in self.members if m.function is function)

    # ------------------------------------------------------------------ roles

    def role_in(self, incident: str) -> Role | None:
        for role in self.roles:
            if role.incident == incident:
                return role
        return None

    @property
    def is_attacker_anywhere(self) -> bool:
        """Deliberately awkward to reach for.

        There is no `is_bad`. Every question about wrongdoing has to name the
        incident, because "this entity attacked something once" and "this
        entity attacked the thing you are screening against" are different
        facts and only the second justifies most actions.
        """
        return any(r.kind is RoleKind.ATTACKER for r in self.roles)

    def controlled_at(self, when: datetime) -> bool:
        """Whether the current controller held these addresses at ``when``.

        False means the entity's *name and roles* do not describe whoever
        controlled it then. It is not a claim about who did --- that is a
        separate entity, if anyone identified one.
        """
        # Checked before the early return, not after. With the order reversed a
        # naive timestamp raised for an entity whose control had changed and
        # passed silently for every other one --- so the validation fired on the
        # rare case and let the common one through, which is the opposite of
        # useful. A caller comparing a naive local time against an aware
        # `controlled_since` is wrong whichever entity they hold.
        if when.tzinfo is None:
            raise EntityError("when must be timezone-aware")
        if self.controlled_since is None:
            return True
        return when >= self.controlled_since

    # ---------------------------------------------------------------- editing

    def with_member(self, member: Membership) -> Entity:
        """A new entity including ``member``. Refuses to overwrite silently."""
        if self.member_for(member.address, member.chain) is not None:
            raise EntityError(
                f"{member.address} is already in {self.key}. Replacing a "
                f"membership discards why it was believed; build a new entity "
                f"if the reason changed"
            )
        return Entity(
            key=self.key,
            name=self.name,
            category=self.category,
            members=(*self.members, member),
            roles=self.roles,
            controlled_since=self.controlled_since,
            note=self.note,
        )

    def with_role(self, role: Role) -> Entity:
        if self.role_in(role.incident) is not None:
            raise EntityError(
                f"{self.key} already has a role in {role.incident}. An entity "
                f"is not both the attacker and the victim of one incident, and "
                f"if the earlier role was wrong it should be corrected "
                f"deliberately"
            )
        return Entity(
            key=self.key,
            name=self.name,
            category=self.category,
            members=self.members,
            roles=(*self.roles, role),
            controlled_since=self.controlled_since,
            note=self.note,
        )


def from_attributions(
    key: str,
    name: str,
    claims: Iterable[Attribution],
    *,
    category: Category = Category.UNKNOWN,
) -> Entity:
    """Build an entity from per-address attributions, keeping each one's source.

    The lossy version of this --- take the addresses, drop everything else ---
    is what makes an entity undefendable the moment somebody asks about one
    member. Every membership keeps the confidence, source and date of the claim
    it came from.
    """
    members: list[Membership] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        if claim.chain is None:
            raise EntityError(
                f"{claim.address} has no chain. An address without one cannot "
                f"be compared safely: folding case is right for EVM and "
                f"destroys a base58 address"
            )
        identity = (str(claim.chain), address_key(claim.chain, claim.address))
        if identity in seen:
            continue
        seen.add(identity)
        members.append(
            Membership(
                address=claim.address,
                chain=claim.chain,
                confidence=claim.confidence,
                source=claim.source,
                observed_at=claim.observed_at,
                rationale=claim.rationale,
            )
        )
    return Entity(key=key, name=name, category=category, members=tuple(members))
