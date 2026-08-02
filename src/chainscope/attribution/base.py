"""What an attribution source is.

A source answers "what is known about this address" and says where it got it.
Sources are independent and expected to disagree; reconciling them is the
resolver's job, not theirs.

Every source declares its licensing posture alongside its data. That is not
bureaucracy: users of this tool publish findings, and "can I redistribute this
label" is a question they need answered at the point of use, not buried in a
README nobody reads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = ["Source", "SourceError", "SourceMeta"]


class SourceError(RuntimeError):
    """A source could not answer.

    Distinct from "answered, found nothing" --- and the resolver keeps them
    distinct, because a sanctions source that silently failed looks exactly like
    a clean result.
    """


@dataclass(frozen=True, slots=True)
class SourceMeta:
    """Provenance for a whole source, as opposed to a single claim."""

    publisher: str
    license: str
    redistributable: bool
    url: str = ""
    snapshot: datetime | None = None
    max_confidence: Confidence = Confidence.HIGH
    """Ceiling this source may assert.

    A community-maintained list does not get to claim CERTAIN just because its
    adapter passes that value in. Enforced in :meth:`Source.emit`.
    """

    #: What a source string carries where a snapshot date would go.
    #:
    #: One word, because there were two. `citation()` said "undated" and
    #: `Source.emit` said "unknown" for the same state, in the same file --- and
    #: source strings are grouped and compared, so two spellings of one fact
    #: read as two facts about two sources.
    #:
    #: "undated" rather than "unknown": the source has no snapshot at all, which
    #: is different from one that exists and could not be read. And it does not
    #: look like a date, so nothing downstream will try to parse it as one.
    UNDATED: ClassVar[str] = "undated"

    def stamp(self) -> str:
        """The snapshot date, or :data:`UNDATED`."""
        return self.snapshot.strftime("%Y-%m-%d") if self.snapshot else self.UNDATED

    def citation(self) -> str:
        return f"{self.publisher} ({self.stamp()}, {self.license})"


class Source(ABC):
    """Base class for attribution sources."""

    name: str = "unnamed"
    meta: SourceMeta
    offline: bool = False
    """True when the source answers from local data.

    Offline sources are queried first and cannot fail the way a network source
    can, which matters when the alternative is silently under-reporting.
    """

    @abstractmethod
    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """Claims about one address. Empty list means "nothing known"."""

    def lookup_many(
        self, addresses: Iterable[str], chain: ChainId | None = None
    ) -> dict[str, list[Attribution]]:
        """Batch lookup.

        The default loops. Override when the underlying source supports batching
        --- sweeping fifty addresses one HTTP request at a time is the difference
        between a usable tool and an unusable one.
        """
        return {a: self.lookup(a, chain) for a in addresses}

    def ready(self) -> bool:
        """Whether this source can answer right now.

        A source whose data file is missing should say so here rather than
        return empty results that read as "this address is clean".
        """
        return True

    def emit(
        self,
        *,
        address: str,
        chain: ChainId | None,
        label: str,
        category: Category,
        confidence: Confidence,
        method: Method,
        rationale: str = "",
        tags: frozenset[str] = frozenset(),
    ) -> Attribution:
        """Build an :class:`Attribution` stamped with this source's identity.

        Sources should use this rather than constructing directly. It applies
        the ``max_confidence`` ceiling and versions the source string, so a
        claim can be traced back to which snapshot produced it --- or, when the
        source carries no snapshot, says `undated` rather than implying one.
        `observed_at` is then `None`, which is the same fact stated in the
        field that is meant to hold it.
        """
        capped = min(confidence, self.meta.max_confidence)
        if capped != confidence:
            rationale = (
                f"{rationale} (confidence capped from {confidence.name} to "
                f"{capped.name} by source policy)"
            ).strip()
        return Attribution(
            address=address,
            chain=chain,
            label=label,
            category=category,
            confidence=capped,
            method=method,
            source=f"{self.name}@{self.meta.stamp()}",
            observed_at=self.meta.snapshot,
            rationale=rationale,
            tags=tags,
        )

    def __repr__(self) -> str:
        where = "offline" if self.offline else "network"
        return f"<{type(self).__name__} {self.name} ({where})>"
