"""What was decided about a deposit, and everything needed to defend it.

The artifact customers actually buy. Everything else in this package exists to
produce one of these and to make it survive being questioned six months later,
which is the only test that matters — a decision nobody challenges did not need
to be defensible, and by the time one is challenged it is too late to add the
provenance.

So the record carries, structurally rather than by convention:

* **which rule fired**, from **which policy at which version** — not a score,
  because "rule `sanctions-direct` fired" is defensible and "the weighted sum
  crossed 0.78" is not;
* **the exposures it rested on**, each with its own evidence, hop distance and
  the reason its trace stopped;
* **the counterfactual** — what would have had to be false for the answer to
  differ. This is the single most useful line for the person signing the
  freeze, and no closed vendor offers it, because offering it means opening the
  scoring;
* **what could not be read**, because a source that never answered must not
  read as a source that answered "nothing".

`Action.ALLOW` is the one value with a structural precondition: it cannot be
recorded on an incomplete screen. See `Decision.__post_init__`. Every other
action is available at any level of ignorance, since holding something you do
not understand is always defensible and releasing it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .exposure import Exposure, Screen

__all__ = ["Action", "Counterfactual", "Decision", "DecisionError"]


class DecisionError(ValueError):
    """A decision was asked to record something indefensible."""


class Action(str, Enum):
    """What to do with the value.

    Deliberately a small closed set of *operations*, not a risk band. A band
    ("high risk") still leaves somebody to decide what high risk means today,
    which is the decision, and pushing it to the customer while charging for
    the band is how a vendor avoids ever being wrong.
    """

    ALLOW = "allow"
    """Credit it. The only action with a precondition: the screen must be
    complete."""

    HOLD = "hold"
    """Do not credit yet. Reversible, and the correct answer to ignorance."""

    ENHANCED_KYC = "enhanced_kyc"
    """Credit only after asking the customer for more. A hold with a route
    out of it, which matters because an indefinite hold on a legitimate
    customer is its own harm."""

    ESCALATE = "escalate"
    """A human decides. The honest answer when the rules do not cover the
    shape, rather than defaulting to the safest-looking option and calling it
    policy."""

    REJECT = "reject"
    """Refuse it. Return-to-sender where possible."""

    REPORT = "report"
    """File with the relevant authority. Orthogonal to the others in
    principle --- reporting does not itself decide whether to credit --- and
    kept in the same set because a policy that cannot express "credit it and
    report it" produces a workflow people route around."""

    @property
    def releases_funds(self) -> bool:
        return self is Action.ALLOW

    @property
    def is_irreversible(self) -> bool:
        """Whether taking this back is not in the customer's gift.

        Rejecting a deposit and filing a report are both things that reach
        outside the institution --- one returns somebody's money to an address
        that may no longer be theirs, the other puts a name in front of an
        authority. Neither can be undone by deciding differently tomorrow, and
        that is the reason they need attributed evidence rather than a shape.
        """
        return self in (Action.REJECT, Action.REPORT)


@dataclass(frozen=True, slots=True)
class Counterfactual:
    """One thing that, had it been absent, would have changed the answer.

    Computed by re-running the policy with a single piece of evidence removed.
    Cheap, and the line a compliance officer needs most: *"this is a hold
    because of one OFAC tag on one address three hops away, and without it the
    answer is allow"* is a sentence somebody can act on, argue with, or take to
    the customer. A score cannot be argued with, which is usually presented as
    a feature.
    """

    without: str
    """What was removed --- an attribution source, or a source and address."""

    then: Action
    """What the policy would have returned instead."""

    note: str = ""


@dataclass(frozen=True, slots=True)
class Decision:
    """The record. Append-only by convention; never edited in place.

    A later re-score produces a *new* decision referencing this one rather than
    replacing it, because "what did you know at the time" is the question that
    gets asked, and a store that only holds current belief cannot answer it.
    """

    action: Action
    screen: Screen

    policy_name: str
    policy_version: int
    """Recorded, never inferred from the current file. A policy that changes
    must not silently restate what was decided under the old one."""

    rule_id: str
    """Which rule fired. Empty only when the policy's default was used, and
    `because` then carries the default's justification."""

    because: str
    """The rule's own justification, copied at decision time. Copied rather
    than referenced because the rule may be edited and the decision may not."""

    decided_at: datetime
    counterfactuals: tuple[Counterfactual, ...] = ()
    attestation: str = ""
    """Hash of the responses this rests on, from `chainscope attest`. Empty
    means nobody bound it, which is a weaker record and says so."""

    supersedes: str | None = None
    """The decision this re-scores, if any."""

    analyst: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.decided_at.tzinfo is None:
            raise DecisionError("decided_at must be timezone-aware")
        if not self.because.strip():
            raise DecisionError(
                "a decision needs its justification. The rule's reasoning is "
                "what gets read when this is challenged, and a rule id alone "
                "sends the reader to a file that may since have changed"
            )
        if self.action.is_irreversible and not self.screen.exposures:
            # The rule that makes real-time screening safe to run at all.
            #
            # At the moment of an exploit no attribution exists --- nobody has
            # labelled the attacker yet --- so everything a screen can see is
            # behavioural: a fresh address, funded once, forwarding everything,
            # a test payment before the real one. Those are `Signal`s, capped
            # at MEDIUM, and they are genuinely useful: they turn "we had no
            # idea" into "we held it for review".
            #
            # What they cannot do is justify rejecting somebody's money or
            # naming them to an authority. A new address forwarding everything
            # is also what a person moving wallets looks like, and the cost of
            # being wrong is asymmetric: a hold is an inconvenience and a
            # report is a permanent record about a real person.
            raise DecisionError(
                f"`{self.action.value}` cannot rest on behavioural signals "
                f"alone --- nothing here is attributed to anyone. A shape is "
                f"consistent with innocent explanations, and this action "
                f"cannot be undone by deciding differently tomorrow. `hold`, "
                f"`enhanced_kyc` and `escalate` are all available and all "
                f"honest about what is actually known"
            )
        if self.action.releases_funds and not self.screen.complete:
            # The one structural refusal in this module.
            #
            # Releasing funds on an incomplete screen is asserting that nothing
            # was found, when what happened is that not everything was looked
            # at. Every other action stays available: holding something you do
            # not understand is always defensible.
            missing = ", ".join(self.screen.unreachable_sources) or (
                "a trace stopped before reaching the origin"
            )
            raise DecisionError(
                f"cannot record `allow` on an incomplete screen ({missing}). "
                f"An absent answer is not a clean one --- use `hold` or "
                f"`escalate`, which are honest about the gap"
            )

    @property
    def rests_on(self) -> tuple[Exposure, ...]:
        return self.screen.exposures

    @property
    def is_defensible(self) -> bool:
        """Whether this carries everything a challenge will ask for.

        Not a claim that the decision is *correct*. It is a claim that the
        reasoning can be reconstructed: the policy version, the rule, its
        justification, and a binding to the data. A decision that fails this is
        not wrong, it is unreviewable, which is worse in the one setting that
        matters.
        """
        return bool(
            self.policy_name
            and self.policy_version
            and self.because.strip()
            and self.attestation.strip()
        )

    def explain(self) -> str:
        """The whole decision, for a human who has to sign or defend it."""
        lines = [
            f"{self.action.value.upper()} — {self.screen.address}",
            f"  rule      {self.rule_id or '(policy default)'} "
            f"({self.policy_name} v{self.policy_version})",
            f"  because   {self.because}",
        ]
        if self.screen.exposures:
            lines.append("  exposure")
            for item in self.screen.exposures:
                where = "direct" if item.hops == 0 else f"{item.hops} hop(s)"
                lines.append(
                    f"    {item.share:>6.1%} {item.category.value} via "
                    f"{item.source.name} ({where})"
                )
                for claim in item.evidence[:2]:
                    when = claim.observed_at.date() if claim.observed_at else "undated"
                    lines.append(f"           {claim.source}, {claim.confidence.name}, {when}")
                if not item.is_conclusive:
                    lines.append(
                        f"           trace stopped: {item.stopped_at.value} — "
                        f"this path is a prefix, not a conclusion"
                    )
        else:
            lines.append("  exposure  none attributed")
        if self.screen.signals:
            lines.append("  signals   (shape only --- nobody has attributed this)")
            for signal in self.screen.signals:
                lines.append(
                    f"    {signal.confidence.name:<11} {signal.name}: {signal.summary}"
                )
        if not self.screen.complete:
            gaps = ", ".join(self.screen.unreachable_sources) or "a trace stopped early"
            lines.append(f"  incomplete {gaps}")
        for what_if in self.counterfactuals:
            lines.append(f"  without   {what_if.without} → {what_if.then.value}")
        lines.append(
            f"  attested  {self.attestation or 'no — this record is not bound to its data'}"
        )
        return "\n".join(lines)
