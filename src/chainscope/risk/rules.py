"""A policy to start from, and a way to read one off disk.

The policy layer takes rules as data on purpose --- an institution's tolerance
is its own to write down, and a vendor that ships thresholds and calls them
"the model" has taken the decision while leaving the liability behind. But
shipping *nothing* has a failure mode too: the first person to run
`chainscope screen` gets a tool with no rules, every screen falls through to
the default `HOLD`, and the honest-but-useless answer looks like a bug.

So there is a starter policy. Two things about it, both deliberate:

**It is version 1 of a policy named ``starter``, not "the chainscope model".**
The name travels into every decision record, so a report produced under it says
plainly that nobody at the institution had yet written a rule set. That is
true, and it is the sentence an auditor should see.

**Every threshold in it is arbitrary and says so.** There is no published hop
count at which indirect exposure becomes actionable, and any number here is a
placeholder for a judgement the customer has not made yet. The `because` on
each rule states that, so a rule that was never reviewed cannot be mistaken for
one that was.

The YAML form is the same shape with no expression language --- see `When` for
why a rule you can only read by executing it is not reviewable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.attribution import Category, Confidence
from ..core.entity import RoleKind
from .decision import Action
from .policy import Policy, PolicyError, Rule, When

__all__ = ["load_policy", "starter_policy"]


def starter_policy() -> Policy:
    """Ordered rules covering the shapes a screen actually produces.

    Order is the semantics. Sanctions first because the answer there is not
    proportional to amount; the incomplete-screen rule sits above the
    permissive tail so that a hold for ignorance is never overtaken by a rule
    that would have allowed; and the unattributed-signal rule is last of the
    substantive ones because a shape must not pre-empt a fact.
    """
    return Policy(
        name="starter",
        version=1,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        rules=(
            Rule(
                id="sanctioned-direct",
                when=When(categories=frozenset({Category.SANCTIONED}), max_hops=0),
                then=Action.REJECT,
                because=(
                    "The counterparty itself is on a sanctions list. This is the "
                    "one case where the amount does not enter into it."
                ),
            ),
            Rule(
                id="sanctioned-indirect",
                when=When(categories=frozenset({Category.SANCTIONED})),
                then=Action.ESCALATE,
                because=(
                    "Sanctioned exposure at a distance. Escalated rather than "
                    "rejected because no regulator has published a hop count at "
                    "which indirect exposure becomes the same fact as direct, and "
                    "inventing one here would be this tool deciding."
                ),
            ),
            Rule(
                id="attacker-role",
                when=When(roles=frozenset({RoleKind.ATTACKER})),
                then=Action.ESCALATE,
                because=(
                    "An entity recorded as the attacker in a named incident. A "
                    "role is always bound to an incident here, so this is a claim "
                    "about an event rather than about a person."
                ),
            ),
            Rule(
                id="mixer-material",
                when=When(
                    categories=frozenset({Category.MIXER}),
                    min_share=Decimal("0.05"),
                ),
                then=Action.ENHANCED_KYC,
                because=(
                    "A material fraction came through a mixer. 5% is a placeholder "
                    "for a judgement the institution has not made; enhanced KYC "
                    "rather than a hold because a mixer has legitimate uses and an "
                    "indefinite hold on a legitimate customer is its own harm."
                ),
            ),
            Rule(
                id="illicit-well-sourced",
                when=When(
                    categories=frozenset({Category.ILLICIT, Category.SCAM}),
                    min_confidence=Confidence.HIGH,
                ),
                then=Action.ESCALATE,
                because=(
                    "Illicit exposure resting on a high-confidence claim. The "
                    "confidence floor is the point: the same category asserted by a "
                    "heuristic is a lead, and leads are reviewed, not acted on."
                ),
            ),
            Rule(
                id="incomplete",
                when=When(when_incomplete=True),
                then=Action.HOLD,
                because=(
                    "Something could not be read --- a source failed, or the walk "
                    "stopped at a service or a hop limit. An absent answer is not a "
                    "clean one, and this rule exists so that the reason is stated "
                    "rather than left to the safety floor to apply silently."
                ),
            ),
            Rule(
                id="shape-only",
                when=When(when_unattributed=True),
                then=Action.HOLD,
                because=(
                    "Behavioural signals fired and nobody has attributed anything. "
                    "This is what an incident looks like on the day it happens, "
                    "before any analyst has labelled the address. A hold converts "
                    "'we had no idea' into 'we held it for review'."
                ),
            ),
            Rule(
                id="exchange-only",
                when=When(categories=frozenset({Category.CEX}), max_hops=0),
                then=Action.ALLOW,
                because=(
                    "Funded directly by a custodial exchange, which runs its own "
                    "KYC. Allowed only if the screen is otherwise complete --- the "
                    "safety floor in `Policy.choose` reduces this to a hold if it "
                    "is not."
                ),
            ),
        ),
        default=Action.HOLD,
    )


_ACTIONS = {a.value: a for a in Action}
_CATEGORIES = {c.value: c for c in Category}
_ROLES = {r.value: r for r in RoleKind}
_CONFIDENCE = {c.name.lower(): c for c in Confidence}


def _enum(table: dict[str, Any], value: str, what: str, rule: str) -> Any:
    try:
        return table[str(value).strip().lower()]
    except KeyError:
        raise PolicyError(
            f"rule {rule!r}: {value!r} is not a known {what}. "
            f"One of: {', '.join(sorted(table))}"
        ) from None


def _when(raw: dict[str, Any], rule: str) -> When:
    unknown = set(raw) - {
        "categories",
        "roles",
        "max_hops",
        "min_share",
        "min_confidence",
        "signals",
        "when_incomplete",
        "when_unattributed",
    }
    if unknown:
        # Refused rather than ignored. A misspelt condition that is silently
        # dropped makes the rule fire on *more* than intended, and a policy
        # that quietly widened is worse than one that failed to load.
        raise PolicyError(
            f"rule {rule!r}: unknown condition(s) {', '.join(sorted(unknown))}. "
            f"A condition this does not understand would be ignored, and an "
            f"ignored condition makes the rule match more than it says"
        )
    return When(
        categories=frozenset(
            _enum(_CATEGORIES, c, "category", rule) for c in raw.get("categories", ())
        ),
        roles=frozenset(_enum(_ROLES, r, "role", rule) for r in raw.get("roles", ())),
        max_hops=raw.get("max_hops"),
        min_share=Decimal(str(raw["min_share"])) if "min_share" in raw else None,
        min_confidence=(
            _enum(_CONFIDENCE, raw["min_confidence"], "confidence", rule)
            if "min_confidence" in raw
            else None
        ),
        signals=frozenset(raw.get("signals", ())),
        when_incomplete=bool(raw.get("when_incomplete", False)),
        when_unattributed=bool(raw.get("when_unattributed", False)),
    )


def load_policy(path: Path | str) -> Policy:
    """Read a policy from YAML.

    The file is the customer's, so this validates loudly and guesses at
    nothing: an unknown category, a missing `because`, a duplicate rule id and
    an unrecognised condition are all errors. A policy that loaded with parts
    of it dropped would produce decisions nobody could reconstruct.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - depends on the extra
        raise PolicyError(
            "reading a policy from YAML needs PyYAML: pip install 'chainscope[all]'"
        ) from None

    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: a policy file is a mapping with name, version, rules")

    rules = []
    for i, item in enumerate(raw.get("rules") or ()):
        if not isinstance(item, dict):
            raise PolicyError(f"{path}: rule {i} is not a mapping")
        rule_id = str(item.get("id") or f"rule-{i}")
        if "then" not in item:
            raise PolicyError(f"rule {rule_id!r} has no `then`: it never says what to do")
        rules.append(
            Rule(
                id=rule_id,
                when=_when(item.get("when") or {}, rule_id),
                then=_enum(_ACTIONS, item["then"], "action", rule_id),
                because=str(item.get("because", "")),
            )
        )

    effective = raw.get("effective_from")
    if isinstance(effective, str):
        parsed = datetime.fromisoformat(effective)
        effective = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    elif isinstance(effective, datetime) and effective.tzinfo is None:
        effective = effective.replace(tzinfo=timezone.utc)

    return Policy(
        name=str(raw.get("name") or Path(path).stem),
        version=int(raw.get("version", 1)),
        rules=tuple(rules),
        default=_enum(_ACTIONS, raw.get("default", "hold"), "action", "default"),
        effective_from=effective,
    )
