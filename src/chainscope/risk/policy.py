"""The customer's rules, in order, with a version on them.

Ordered rules rather than weights, and the reason is what happens when the
decision is challenged. *"Rule `sanctions-direct` fired, and here is the
sentence explaining why that rule exists"* is defensible. *"The weighted sum
crossed 0.78"* invites the question of where 0.78 came from, and the honest
answer is usually that somebody tuned it until the alert volume looked
reasonable.

**The thresholds belong to the customer, not to us.** No regulator has
published a hop limit or a de minimis exposure level; OFAC has published
neither. And setting one too high has already been penalised --- the NYDFS
action against Block turned partly on internal thresholds, where even a 1%
exposure to terrorism-linked wallets was not defensible as "below tolerance".
A vendor default applied silently is precisely the thing that failed. So a
policy is data the customer owns, carries a version, and the version travels
into every decision it produces.

**Editing a policy never rewrites the past.** Decisions record the version that
produced them. Re-scoring under a new version produces a *new* decision that
supersedes the old one, and both stay readable.

**Two floors apply after the rules and can only make the answer more
conservative.** They exist because a rule author cannot be relied on to
remember them at three in the morning:

* `allow` on an incomplete screen becomes `hold` --- an absent answer is not a
  clean one;
* an irreversible action resting on behavioural signals alone becomes
  `escalate` --- a shape cannot justify returning somebody's money or naming
  them to an authority.

Both record that they fired, in the decision's own words. A floor that silently
adjusted the answer would be the same defect as a silent threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from ..core.attribution import Category, Confidence
from ..core.entity import RoleKind
from .decision import Action, Counterfactual, Decision
from .exposure import Exposure, Screen, Signal

__all__ = ["Match", "Policy", "PolicyError", "Rule", "When"]


class PolicyError(ValueError):
    """A policy was asked to represent something indefensible."""


@dataclass(frozen=True, slots=True)
class When:
    """What has to be true for a rule to fire.

    Every field is optional and an unset field matches anything, so a `When`
    with nothing set matches any screen --- which is how a default is written.
    Set fields are ANDed; the collections are ORed within themselves.

    Deliberately no free-form expression language. A rule somebody can only
    read by executing it is not a rule a compliance officer can review, and
    reviewability is the entire point of putting the policy in the customer's
    hands.
    """

    categories: frozenset[Category] = frozenset()
    roles: frozenset[RoleKind] = frozenset()
    max_hops: int | None = None
    """Inclusive. 0 means direct exposure only."""

    min_share: Decimal | None = None
    min_confidence: Confidence | None = None
    """Of the *evidence*. A rule can require that only well-sourced claims
    trigger it, which is how an institution says "act on OFAC, review
    heuristics"."""

    signals: frozenset[str] = frozenset()
    """Analyzer names, matched against `Screen.signals`. A rule naming signals
    matches on shape rather than attribution --- see `Signal`."""

    when_incomplete: bool = False
    """Fires when a source could not be read or a trace stopped early."""

    when_unattributed: bool = False
    """Fires when signals exist and nothing is attributed. The t=0 state."""

    def matches_exposure(self, item: Exposure) -> bool:
        if self.categories and item.category not in self.categories:
            return False
        if self.roles and item.role not in self.roles:
            return False
        if self.max_hops is not None and item.hops > self.max_hops:
            return False
        if self.min_share is not None and item.share < self.min_share:
            return False
        if self.min_confidence is not None:
            best = max((claim.confidence for claim in item.evidence), default=None)
            if best is None or best < self.min_confidence:
                return False
        return True

    def matches_signal(self, item: Signal) -> bool:
        if not self.signals:
            return False
        if item.name not in self.signals:
            return False
        return not (self.min_confidence is not None and item.confidence < self.min_confidence)

    @property
    def is_about_exposures(self) -> bool:
        """Whether this looks at attributed exposure at all.

        A rule that only names signals or screen properties must not be tested
        against exposures, or it would fire on every screen that has any.
        """
        return bool(
            self.categories
            or self.roles
            or self.max_hops is not None
            or self.min_share is not None
        )


@dataclass(frozen=True, slots=True)
class Rule:
    """One ordered rule. First match wins."""

    id: str
    when: When
    then: Action
    because: str
    """Why this rule exists, in the words of whoever wrote it.

    Required, and copied into every decision the rule produces. This is the
    sentence read six months later, and a rule id on its own sends the reader
    to a file that may since have changed."""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise PolicyError("a rule needs an id")
        if not self.because.strip():
            raise PolicyError(
                f"rule {self.id!r} needs a justification. Every decision it "
                f"produces will carry this sentence, and it is what a "
                f"challenge is answered with"
            )


@dataclass(frozen=True, slots=True)
class Match:
    """Which rule fired and what it fired on."""

    rule: Rule
    exposures: tuple[Exposure, ...] = ()
    signals: tuple[Signal, ...] = ()
    on_screen_property: str = ""


@dataclass(frozen=True, slots=True)
class Policy:
    """A named, versioned, ordered rule set owned by the customer."""

    name: str
    version: int
    rules: tuple[Rule, ...] = ()
    default: Action = Action.HOLD
    """What happens when nothing matches.

    `HOLD` rather than `ALLOW`, because a rule set that has not considered a
    case has not cleared it. An institution wanting the other default has to
    write it down, which is the point."""

    default_because: str = (
        "No rule matched. A policy that has not considered this shape has not cleared it."
    )
    effective_from: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PolicyError("a policy needs a name")
        if self.version < 1:
            raise PolicyError("policy versions start at 1")
        if self.effective_from is not None and self.effective_from.tzinfo is None:
            raise PolicyError("effective_from must be timezone-aware")
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise PolicyError(
                    f"two rules share the id {rule.id!r}. A decision records "
                    f"which rule fired, and an ambiguous id makes that record "
                    f"useless"
                )
            seen.add(rule.id)

    # ------------------------------------------------------------- matching

    def match(self, screen: Screen) -> Match | None:
        """The first rule that fires, or None.

        Order is the whole semantics. Rules are not scored and not combined:
        reordering a policy changes its behaviour, which is a property of
        ordered rules rather than a bug in them, and it is why the version
        matters.
        """
        for rule in self.rules:
            hit_exposures = (
                tuple(e for e in screen.exposures if rule.when.matches_exposure(e))
                if rule.when.is_about_exposures
                else ()
            )
            hit_signals = tuple(s for s in screen.signals if rule.when.matches_signal(s))
            on_property = ""
            if rule.when.when_incomplete and not screen.complete:
                on_property = "screen incomplete"
            elif rule.when.when_unattributed and screen.unattributed:
                on_property = "nothing attributed"

            if hit_exposures or hit_signals or on_property:
                return Match(
                    rule=rule,
                    exposures=hit_exposures,
                    signals=hit_signals,
                    on_screen_property=on_property,
                )
        return None

    # ------------------------------------------------------------ deciding

    def choose(self, screen: Screen) -> tuple[Action, str, str, tuple[str, ...]]:
        """``(action, rule_id, because, notes)`` without building a record.

        Separate from `decide` so the counterfactual can re-run the policy
        cheaply, and so the safety floors are applied in exactly one place.
        """
        found = self.match(screen)
        if found is None:
            action, rule_id, because = self.default, "", self.default_because
        else:
            action, rule_id, because = found.rule.then, found.rule.id, found.rule.because

        notes: list[str] = []

        # Floors. They only ever make the answer more conservative, and they
        # say so --- a floor that adjusted the answer silently would be the
        # same defect as a threshold nobody wrote down.
        if action.releases_funds and not screen.complete:
            gaps = ", ".join(screen.unreachable_sources) or "a trace stopped early"
            notes.append(
                f"`allow` was reduced to `hold`: the screen is incomplete ({gaps}), "
                f"and an absent answer is not a clean one."
            )
            action = Action.HOLD
        if action.is_irreversible and not screen.exposures:
            notes.append(
                f"`{action.value}` was reduced to `escalate`: nothing here is "
                f"attributed to anyone, and a shape cannot justify returning "
                f"somebody's money or naming them to an authority."
            )
            action = Action.ESCALATE

        return action, rule_id, because, tuple(notes)

    def decide(
        self,
        screen: Screen,
        *,
        at: datetime,
        analyst: str = "",
        attestation: str = "",
        supersedes: str | None = None,
        counterfactuals: bool = True,
    ) -> Decision:
        """Apply the policy and record the result."""
        action, rule_id, because, notes = self.choose(screen)
        what_ifs = self.counterfactuals(screen, action) if counterfactuals else ()
        return Decision(
            action=action,
            screen=screen,
            policy_name=self.name,
            policy_version=self.version,
            rule_id=rule_id,
            because=because,
            decided_at=at,
            counterfactuals=what_ifs,
            attestation=attestation,
            supersedes=supersedes,
            analyst=analyst,
            notes=notes,
        )

    # ------------------------------------------------------- counterfactual

    def counterfactuals(self, screen: Screen, action: Action) -> tuple[Counterfactual, ...]:
        """What would have had to be absent for the answer to differ.

        Computed by removing one piece of evidence at a time and re-running.
        Cheap --- the policy is a list of comparisons over data already in
        memory --- and it produces the line a compliance officer needs most:
        *"this is a hold because of one OFAC tag on an address three hops away,
        and without it the answer is allow"*.

        That sentence can be argued with, taken to the customer, or acted on. A
        score cannot be argued with, which is usually presented as a feature.

        Only removals that **change the action** are reported. A list of things
        that made no difference is noise, and the reader is looking for the
        load-bearing one.
        """
        out: list[Counterfactual] = []

        for source in _evidence_sources(screen.exposures):
            without = replace(
                screen,
                exposures=tuple(e for e in screen.exposures if not _rests_only_on(e, source)),
            )
            other, *_ = self.choose(without)
            if other is not action:
                out.append(
                    Counterfactual(
                        without=source,
                        then=other,
                        note="the only source behind at least one exposure",
                    )
                )

        for signal in screen.signals:
            without = replace(
                screen, signals=tuple(s for s in screen.signals if s is not signal)
            )
            other, *_ = self.choose(without)
            if other is not action:
                out.append(
                    Counterfactual(
                        without=f"signal {signal.name}",
                        then=other,
                        note=signal.summary,
                    )
                )

        return tuple(out)


def _evidence_sources(exposures: Iterable[Exposure]) -> tuple[str, ...]:
    """Distinct attribution sources, in first-seen order.

    Ordered rather than a set, so the counterfactual list is stable between
    runs --- two screenings of the same deposit have to produce the same record
    or the record is not reproducible.
    """
    seen: list[str] = []
    for item in exposures:
        for claim in item.evidence:
            if claim.source not in seen:
                seen.append(claim.source)
    return tuple(seen)


def _rests_only_on(item: Exposure, source: str) -> bool:
    """Whether removing ``source`` would leave this exposure unevidenced.

    An exposure corroborated by two sources does not vanish when one is
    removed, and reporting that it would would overstate how much rests on any
    single tag --- which is the opposite of what the counterfactual is for.
    """
    return all(claim.source == source for claim in item.evidence)
