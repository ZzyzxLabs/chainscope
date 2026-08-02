"""Scored inferences.

Cross-chain matching, change-output detection, and clustering are heuristics.
Returning "the answer" from a heuristic is precisely how a guess becomes a fact
somewhere downstream, so this module makes that shape unavailable: inference
returns ranked candidates with the arithmetic exposed.

The practical test is whether a reviewer who disagrees with a ranking can see
*which* factor they disagree with. If the output is a bare answer, they cannot,
and the only options are to trust it or discard it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .attribution import Confidence

__all__ = ["Hypothesis", "ScoreFactor"]


@dataclass(frozen=True, slots=True)
class ScoreFactor:
    """One named contribution to a score.

    ``weight`` may be negative --- evidence against a hypothesis is evidence, and
    hiding it produces a score nobody can argue with.
    """

    name: str
    weight: float
    value: Any
    note: str = ""

    @property
    def contribution(self) -> float:
        """Weight applied if the factor holds, zero if it does not.

        Truthiness is deliberate: factors are usually booleans, but a numeric
        value that happens to be ``0`` genuinely contributes nothing.
        """
        return self.weight if self.value else 0.0

    def __str__(self) -> str:
        sign = "+" if self.contribution >= 0 else ""
        tail = f" -- {self.note}" if self.note else ""
        return f"{sign}{self.contribution:g} {self.name}{tail}"


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A candidate conclusion, its score, and how the score was reached."""

    claim: str
    factors: tuple[ScoreFactor, ...] = ()
    confidence: Confidence = Confidence.LOW
    alternatives: tuple[Hypothesis, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    """Whatever the caller needs from the candidate: txid, amount, address."""

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValueError("a hypothesis needs a claim")
        if self.confidence > Confidence.MEDIUM:
            # HIGH and CERTAIN mean "someone published this" or "the chain says
            # so". A scored inference is neither, and letting one claim those
            # levels is the exact confusion this module prevents.
            raise ValueError(
                f"a Hypothesis cannot exceed MEDIUM confidence; got "
                f"{self.confidence.name}. Inference is not a published label."
            )

    @property
    def score(self) -> float:
        return sum(f.contribution for f in self.factors)

    @property
    def supporting(self) -> tuple[ScoreFactor, ...]:
        return tuple(f for f in self.factors if f.contribution > 0)

    @property
    def opposing(self) -> tuple[ScoreFactor, ...]:
        return tuple(f for f in self.factors if f.contribution < 0)

    @property
    def is_contested(self) -> bool:
        """Whether the runner-up scores close enough that ranking is not decisive.

        A caller that ignores this and reports only the top candidate is
        reporting a coin flip as a finding.
        """
        if not self.alternatives:
            return False
        return (self.score - self.alternatives[0].score) < 1.0

    def explain(self) -> str:
        lines = [f"{self.claim}  [score {self.score:g}, {self.confidence.name}]"]
        lines += [f"  {f}" for f in self.factors]
        if self.is_contested:
            runner_up = self.alternatives[0]
            lines.append(
                f"  ! contested: next candidate scores {runner_up.score:g} ({runner_up.claim})"
            )
        return "\n".join(lines)

    def __str__(self) -> str:
        return f"{self.claim} (score {self.score:g})"


def rank(candidates: list[Hypothesis]) -> list[Hypothesis]:
    """Order candidates by score and thread the alternatives through.

    Each returned hypothesis carries the ones below it, so a caller holding only
    the winner can still see what it beat and by how much.
    """
    ordered = sorted(candidates, key=lambda h: -h.score)
    out: list[Hypothesis] = []
    for i, h in enumerate(ordered):
        out.append(
            Hypothesis(
                claim=h.claim,
                factors=h.factors,
                confidence=h.confidence,
                alternatives=tuple(ordered[i + 1 :]),
                data=h.data,
            )
        )
    return out
