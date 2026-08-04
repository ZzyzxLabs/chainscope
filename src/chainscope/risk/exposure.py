"""What a deposit is exposed to, itemised.

The question a customer actually asks is *"can I accept this money"*, and the
shape of a defensible answer is settled by how the answer gets challenged. Six
months later somebody asks: which source said so, how far away was it, how much
of *this* deposit, and would it still have been rejected without that one tag.

A number cannot answer any of those. `risk_score: 82` is not a finding, it is a
summary of findings with the findings thrown away — and the findings are the
part that has to survive. So exposure here is a **list of typed claims**, and
there is deliberately no method on it that reduces the list to a scalar. The
policy layer decides; this layer describes.

Three things this module refuses to do, each because the field has already
demonstrated the cost:

**It will not merge direct and indirect exposure.** A counterparty that is a
sanctioned entity and a counterparty three hops from one are different facts,
and no regulator has published a hop threshold that makes them comparable. They
stay separate items with `hops` on each.

**It will not continue through a custodial service.** Exchanges pool thousands
of unrelated customers through the same addresses, so a path that runs onward
through a hot wallet is an artefact of shared infrastructure rather than a flow.
The trace stops, and `stopped_at` records that it stopped — because a trace that
ended because we stopped looking and one that ended because the money did are
opposite claims.

**It will not let a failed lookup read as a clean result.** `Exposure.complete`
is false when anything could not be read, and the policy layer is expected to
refuse `allow` on an incomplete screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from ..core.attribution import Attribution, Category, Confidence
from ..core.entity import Entity, RoleKind
from ..core.units import Amount

__all__ = [
    "Directness",
    "Exposure",
    "ExposureError",
    "Screen",
    "Signal",
    "StopReason",
]


class ExposureError(ValueError):
    """An exposure was asked to represent something it must not."""


class Directness(str, Enum):
    """How far the exposure sits from the deposit.

    Derived from `hops` rather than set independently, so the two can never
    disagree --- which they do, silently, in every representation that stores
    both.
    """

    DIRECT = "direct"
    """The counterparty itself. Hop 0."""

    INDIRECT = "indirect"
    """Reached through one or more intermediaries."""


class StopReason(str, Enum):
    """Why a backward trace ended.

    The distinction the whole package exists for, at the one point where
    getting it wrong produces a frozen account. ``EXHAUSTED`` is the only value
    that means the money's origin was actually reached.
    """

    EXHAUSTED = "exhausted"
    """Followed to the origin. The trace is complete on this path."""

    SERVICE = "service"
    """Reached a custodial service and stopped deliberately. The money came
    from somewhere before this, through an omnibus account we cannot see
    into --- so this path is a prefix, not a conclusion."""

    HOP_LIMIT = "hop_limit"
    """Ran out of configured depth. Says nothing about what lies beyond."""

    UNREACHABLE = "unreachable"
    """A source or provider failed. The path is short because the lookup was,
    not because the money was."""

    @property
    def is_conclusion(self) -> bool:
        return self is StopReason.EXHAUSTED


@dataclass(frozen=True, slots=True)
class Exposure:
    """One thing this deposit is exposed to, with everything needed to defend
    saying so.

    Every field here answers a question somebody asks when challenged, and the
    ones that look redundant are not: `amount` is how much of *this* deposit
    the trace attributes to that source, `share` is that as a fraction, and a
    policy is written against one or the other depending on whether the
    institution's tolerance is absolute or proportional. Both are commonly
    used; storing one and computing the other at read time is how a rounding
    convention becomes a threshold.
    """

    source: Entity
    """The party the exposure is *to*."""

    category: Category
    amount: Amount
    """How much of the deposit is attributed to this source, by the taint
    model named in `Screen.taint`. Not the source's balance, and not the size
    of the original incident."""

    share: Decimal
    """`amount` as a fraction of the deposit, in [0, 1]."""

    hops: int
    """0 for the counterparty itself."""

    path: tuple[str, ...] = ()
    """The addresses between, in order. The reader has to be able to walk it."""

    role: RoleKind | None = None
    incident: str = ""
    """The role is meaningless without the incident it was played in, and an
    exposure that names a role without one is the accusation this package
    refuses to make."""

    evidence: tuple[Attribution, ...] = ()
    """Whose word this rests on. Empty is not allowed --- see `__post_init__`."""

    stopped_at: StopReason = StopReason.EXHAUSTED
    """Why the trace that found this stopped."""

    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.hops < 0:
            raise ExposureError("hops cannot be negative")
        if not (Decimal(0) <= self.share <= Decimal(1)):
            raise ExposureError(f"share must be in [0, 1]; got {self.share}")
        if not self.evidence:
            raise ExposureError(
                "an exposure with no evidence is an assertion. Every item has "
                "to name the attribution it rests on, because 'which source "
                "said so' is the first question asked of any decision built "
                "from it"
            )
        if self.role is not None and not self.incident.strip():
            raise ExposureError(
                f"role {self.role.value} needs the incident it was played in. "
                f"'Attacker' unqualified is a claim about a person; 'attacker "
                f"in incident X' is a claim about an event"
            )
        if self.hops == 0 and self.path:
            raise ExposureError("a direct exposure has no intermediate path")

    @property
    def directness(self) -> Directness:
        return Directness.DIRECT if self.hops == 0 else Directness.INDIRECT

    @property
    def is_conclusive(self) -> bool:
        """Whether the path behind this exposure was followed to its origin.

        False does not weaken the exposure --- what was found was found. It
        means there may be more that was not looked for, which is a different
        thing and a policy may care about it.
        """
        return self.stopped_at.is_conclusion


@dataclass(frozen=True, slots=True)
class Signal:
    """Something the *shape* of the money says, with nobody vouching for it.

    The gap `Exposure` cannot fill, and the one that decides whether this
    system is useful on the day of an incident rather than a week after it.

    An `Exposure` requires an `Attribution`: somebody, somewhere, has said this
    address is a mixer or a sanctioned entity. At the moment an exploit
    happens, **that sentence does not exist yet**. Nobody has labelled the
    attacker, the analysts are still reading the transaction, and a screening
    system built only on attribution answers "clean" to the most dangerous
    deposit it will see all year --- correctly, and uselessly.

    What does exist at t=0 is behaviour. The depositing address was created
    forty minutes ago. It was funded once and is forwarding everything. It sent
    a small test payment first and the real one straight after. Those are
    observations about structure, and `analysis/` already produces them:
    `probing`, `impersonation`, `linked_holders`, `peel_chain`, `consolidation`.

    **A signal is capped at MEDIUM and cannot support an irreversible action.**
    That is not caution for its own sake. A shape is consistent with a great
    many innocent explanations --- a new address forwarding everything is also
    what a person moving to a new wallet looks like --- and the same reasoning
    that caps `Hypothesis` at MEDIUM applies here for the same reason. The
    policy layer enforces it: a signal can produce `hold`, `enhanced_kyc` or
    `escalate`, and only an attributed exposure can produce `reject`.

    The honest summary of what this buys: it converts "we had no idea" into "we
    held it for review", which is the difference the day a customer's money
    goes out the door.
    """

    name: str
    """The analyzer that produced it, e.g. ``probing``."""

    summary: str
    detail: str = ""
    confidence: Confidence = Confidence.LOW
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.summary.strip():
            raise ExposureError("a signal needs a name and a summary")
        if self.confidence > Confidence.MEDIUM:
            raise ExposureError(
                f"a behavioural signal cannot exceed MEDIUM confidence; got "
                f"{self.confidence.name}. A shape is consistent with innocent "
                f"explanations, and this is the same cap `Hypothesis` carries "
                f"for the same reason. If somebody has actually attributed the "
                f"address, that is an Exposure with evidence, not a signal"
            )


@dataclass(frozen=True, slots=True)
class Screen:
    """Everything found about one deposit, and everything that was not found.

    Deliberately not a score, and deliberately not sorted by severity: ordering
    by badness is already a policy judgement, and this type is the input to
    policy rather than a partial application of it.
    """

    address: str
    amount: Amount
    at: datetime
    """When the value arrived. Every time-sensitive check --- was the entity
    under this control, was the sanction listed yet --- is asked against this
    and never against now."""

    taint: str
    """Which taint model produced the attribution: ``fifo``, ``haircut`` or
    ``poison``. Recorded because the three disagree, so a result that does not
    name one cannot be reproduced.

    FIFO is the default elsewhere in this package, on the grounds that it is
    the rule English law already applies to mixed funds (Clayton's Case, 1816)
    and is therefore the one a customer can defend by citing something other
    than a vendor convention."""

    exposures: tuple[Exposure, ...] = ()
    signals: tuple[Signal, ...] = ()
    """What the shape of the money says, where nobody has attributed anything.

    Kept apart from `exposures` rather than merged, because they carry
    different weight and expire differently: an attribution stays true until
    somebody retracts it, and a signal is superseded the moment a real
    attribution lands. Merging them would let a shape borrow an attribution's
    standing --- which is how a heuristic ends up justifying a freeze."""

    unreachable_sources: tuple[str, ...] = ()
    """Sources that could not be read. Non-empty makes this screen incomplete,
    and a policy must not answer `allow` on the strength of a source that
    never spoke."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Whether anything is known to be missing.

        Two ways to be incomplete and both count: a source that failed, and a
        trace that stopped somewhere other than the origin.
        """
        if self.unreachable_sources:
            return False
        return all(e.is_conclusive for e in self.exposures)

    @property
    def unattributed(self) -> bool:
        """Signals fired but nothing is attributed.

        The state a real-time incident produces before anyone has labelled
        anything, and the one a policy most needs to name: there is something
        to look at and nothing to point at.
        """
        return bool(self.signals) and not self.exposures

    @property
    def clean(self) -> bool:
        """No exposure found **and** nothing was missing.

        The property that does not exist in most tools, where "no exposure
        found" is reported the same whether every source answered or none did.
        A screen with zero exposures and a failed source is not clean; it is
        unknown, and this returns False for it.
        """
        return not self.exposures and not self.signals and self.complete

    def of(self, category: Category) -> tuple[Exposure, ...]:
        return tuple(e for e in self.exposures if e.category is category)

    def within(self, hops: int) -> tuple[Exposure, ...]:
        return tuple(e for e in self.exposures if e.hops <= hops)

    def total_share(self, category: Category | None = None) -> Decimal:
        """Summed share, optionally for one category.

        Not a risk figure. Shares from different sources add only if the taint
        model attributed them disjointly, which FIFO does and haircut does not,
        so this is capped at 1 rather than being allowed to report that 140% of
        a deposit was tainted.
        """
        items = self.exposures if category is None else self.of(category)
        return min(Decimal(1), sum((e.share for e in items), Decimal(0)))
