"""Who owns this address, and how sure are we?

This module exists because of a specific, repeated failure mode in blockchain
forensics: a heuristic guess gets written down, passes through three tools, and
comes out the other end looking like a fact. People get accused on that basis.

So chainscope refuses to represent an attribution without its provenance. There
is no way to construct an :class:`Attribution` that does not say where it came
from and how strong the evidence is, and the renderers surface anything below
``Confidence.HIGH`` as a claim rather than a label.

A worked example of why the distinction matters. Instant-swap services are
routinely identified on one chain and invisible on another:

* The Ethereum hot wallet carries a public block-explorer nametag. Someone else
  did the attribution work and published it.
  → ``Confidence.HIGH``, ``Method.LABEL``.
* The Bitcoin hot wallet of the *same* service has no label anywhere, because
  Bitcoin has no equivalent public nametag database. You identify it by
  behaviour: payouts land in a predictable window after deposits, at a
  consistent discount to spot, always paying a fresh address with change
  returning to the wallet.
  → ``Confidence.LOW``, ``Method.INFERENCE``.

Same entity, utterly different evidentiary weight. A flat ``dict[str, str]``
cannot express that, which is why this module is not a flat dict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum

from .chainid import ChainId

__all__ = [
    "Attribution",
    "Category",
    "Confidence",
    "Method",
    "ResolvedEntity",
]


class Confidence(IntEnum):
    """How much weight this attribution can bear.

    Ordered so that ``>=`` comparisons work: ``if attr.confidence >= Confidence.HIGH``.
    """

    SPECULATIVE = 0
    """A single coincidence. Record it, act on nothing."""

    LOW = 1
    """Behavioural inference --- timing, amounts, fee patterns. Needs corroboration."""

    MEDIUM = 2
    """Structural heuristic --- co-spend clustering, deposit-address consolidation."""

    HIGH = 3
    """Published third-party label, e.g. a block explorer nametag."""

    CERTAIN = 4
    """Authoritative: a sanctions list, or the contract naming itself on-chain."""

    @property
    def is_actionable(self) -> bool:
        """Whether this may be stated as fact rather than as a claim."""
        return self >= Confidence.HIGH

    @property
    def marker(self) -> str:
        return {
            Confidence.CERTAIN: "",
            Confidence.HIGH: "",
            Confidence.MEDIUM: "~",
            Confidence.LOW: "?",
            Confidence.SPECULATIVE: "??",
        }[self]


class Method(str, Enum):
    """How the attribution was arrived at."""

    LIST = "list"
    """Present on an authoritative published list (OFAC SDN, etc.)."""

    LABEL = "label"
    """Copied from a third party that publishes labels (explorer nametags)."""

    ONCHAIN = "onchain"
    """The chain says so --- ``name()``, ``symbol()``, contract metadata."""

    HEURISTIC = "heuristic"
    """Derived by a documented algorithm: clustering, consolidation analysis."""

    INFERENCE = "inference"
    """Human or model judgement from circumstantial evidence."""

    MANUAL = "manual"
    """An analyst asserted it. Provenance is the analyst."""


class Category(str, Enum):
    """What kind of thing this address is.

    Deliberately coarse. Fine-grained taxonomy belongs in ``tags``; this field is
    what traversal algorithms branch on (e.g. "stop at exchanges").
    """

    CEX = "cex"
    DEX = "dex"
    BRIDGE = "bridge"
    MIXER = "mixer"
    SANCTIONED = "sanctioned"
    ILLICIT = "illicit"
    TOKEN = "token"
    CONTRACT = "contract"
    SERVICE = "service"
    MINER = "miner"
    SCAM = "scam"
    SUSPECT = "suspect"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Whether on-chain tracing normally stops here.

        Funds entering custodial or mixing infrastructure leave the transparent
        graph; continuing to traverse produces noise, not evidence.
        """
        return self in _TERMINAL


_TERMINAL = frozenset({Category.CEX, Category.MIXER, Category.BRIDGE, Category.SANCTIONED})


@dataclass(frozen=True, slots=True)
class Attribution:
    """One claim about one address, from one source.

    Multiple attributions for the same address are normal and expected --- an
    address can be simultaneously "Binance 14" (explorer label) and sanctioned
    (OFAC). They are not merged destructively; see :class:`ResolvedEntity`.
    """

    address: str
    chain: ChainId | None
    label: str
    category: Category
    confidence: Confidence
    method: Method
    source: str
    """Stable identifier of origin, ideally versioned: ``"ofac-sdn@2026-08-01"``."""

    observed_at: datetime | None = None
    rationale: str = ""
    """Why. Required in spirit for anything below HIGH; renderers show it."""

    tags: frozenset[str] = field(default_factory=frozenset)
    evidence_ref: str | None = None
    """Key into a case bundle's evidence store, if one backs this claim."""

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("attribution needs a non-empty label")
        if not self.source.strip():
            raise ValueError(
                "attribution needs a source --- an unattributed claim is exactly "
                "what this type exists to prevent"
            )
        if self.confidence <= Confidence.LOW and not self.rationale.strip():
            raise ValueError(
                f"confidence={self.confidence.name} requires a rationale explaining "
                f"the reasoning; got none for {self.label!r}"
            )

    @property
    def display(self) -> str:
        m = self.confidence.marker
        return f"{self.label}{(' ' + m) if m else ''}"

    def __str__(self) -> str:
        return f"{self.display} [{self.category.value}] ({self.source})"


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """The merged view of every attribution known for one address.

    Merging picks a *primary* claim for display but discards nothing: the full
    set stays available so an analyst can see that two sources disagree, which is
    itself a finding.
    """

    address: str
    chain: ChainId | None
    primary: Attribution
    all_claims: tuple[Attribution, ...]

    @property
    def label(self) -> str:
        return self.primary.label

    @property
    def category(self) -> Category:
        return self.primary.category

    @property
    def confidence(self) -> Confidence:
        return self.primary.confidence

    @property
    def is_sanctioned(self) -> bool:
        return any(c.category is Category.SANCTIONED for c in self.all_claims)

    @property
    def is_terminal(self) -> bool:
        return any(c.category.is_terminal for c in self.all_claims)

    @property
    def disputed(self) -> bool:
        """True when sources of comparable strength assign different categories."""
        strong = {c.category for c in self.all_claims if c.confidence >= Confidence.HIGH}
        return len(strong) > 1

    def categories(self) -> frozenset[Category]:
        return frozenset(c.category for c in self.all_claims)

    def __str__(self) -> str:
        base = str(self.primary)
        if self.disputed:
            base += "  ⚠ sources disagree"
        elif len(self.all_claims) > 1:
            base += f"  (+{len(self.all_claims) - 1} more)"
        return base


def merge(claims: Iterable[Attribution]) -> ResolvedEntity | None:
    """Reduce claims about one address to a :class:`ResolvedEntity`.

    Precedence, in order:

    1. Sanctions always win the primary slot. If an address is on a sanctions
       list, that is the single most decision-relevant fact about it and must not
       be buried under a friendlier label.
    2. Otherwise, highest confidence.
    3. Ties break toward the more specific category (``UNKNOWN`` loses), then
       toward the more recent observation.
    """
    items = tuple(claims)
    if not items:
        return None

    def rank(a: Attribution) -> tuple[bool, int, bool, float]:
        return (
            a.category is Category.SANCTIONED,
            int(a.confidence),
            a.category is not Category.UNKNOWN,
            a.observed_at.timestamp() if a.observed_at else 0.0,
        )

    primary = max(items, key=rank)
    return ResolvedEntity(
        address=primary.address,
        chain=primary.chain,
        primary=primary,
        all_claims=tuple(sorted(items, key=rank, reverse=True)),
    )
