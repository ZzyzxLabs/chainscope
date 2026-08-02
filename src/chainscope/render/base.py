"""Turning results into something a person reads.

Renderers exist as a separate layer so that analyzers never format anything.
That is what makes one analysis serve a terminal, a report, and a JSON API
without change.

**Every renderer must preserve the confidence distinction.** Anything below
``Confidence.HIGH`` renders as a claim with its basis visible --- never as a
plain label. A renderer that drops that to look tidier has removed the product:
the whole design exists so a heuristic guess cannot pass downstream looking like
a fact.

:func:`qualify` gives every renderer the same wording for that, so terminal,
Markdown, and JSON output cannot drift apart on the one thing that matters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.attribution import Attribution, Confidence, ResolvedEntity
from ..core.result import Result

__all__ = ["Renderer", "qualify", "qualify_entity"]

#: How a claim at each confidence level must be introduced. Wording is
#: deliberately plain: "possibly" and "appears to be" are what a reader
#: understands, where "MEDIUM confidence" invites them to round it up.
_PREFIX = {
    Confidence.CERTAIN: "",
    Confidence.HIGH: "",
    Confidence.MEDIUM: "probably ",
    Confidence.LOW: "possibly ",
    Confidence.SPECULATIVE: "speculatively ",
}


def qualify(attribution: Attribution) -> str:
    """One line describing a claim, honestly.

    Strong claims read as labels. Weak ones read as claims, and carry their
    rationale, because a reader who cannot see the basis has no way to weigh it.
    """
    prefix = _PREFIX[attribution.confidence]
    text = f"{prefix}{attribution.label}"
    if attribution.confidence.is_actionable:
        return text
    basis = attribution.rationale or attribution.method.value
    return f"{text} [{attribution.confidence.name.lower()} confidence: {basis}]"


def qualify_entity(entity: ResolvedEntity | None) -> str:
    """Same, for a merged entity, surfacing disagreement between sources."""
    if entity is None:
        return "unknown"
    text = qualify(entity.primary)
    if entity.disputed:
        cats = sorted({c.category.value for c in entity.all_claims})
        text += f"  (sources disagree: {', '.join(cats)})"
    return text


class Renderer(ABC):
    """Base class for output formats."""

    name: str = "unnamed"

    @abstractmethod
    def render(self, result: Result) -> str: ...

    def render_all(self, results: list[Result]) -> str:
        return "\n\n".join(self.render(r) for r in results)
