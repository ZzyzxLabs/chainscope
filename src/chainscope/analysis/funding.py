"""Common-funder clustering, for account-model chains.

The multi-input heuristic does not exist on Ethereum: a transaction has one
sender, so no transaction ever demonstrates that two addresses share an owner.
The account-model equivalent asks a different question --- *who paid for this
address to exist* --- and links the addresses that share an answer.

The signal is real. A fresh address has no gas, so somebody has to fund it
before it can do anything, and an operator spinning up twenty addresses funds
them from one place because that is the path of least effort. Deployer graphs,
scam-factory infrastructure, and consolidation networks all show up this way.

**And it is dangerous in exactly one direction.** An exchange withdrawal funds
its customers' addresses too. Measured on a synthetic world of twenty operators
and one exchange: without a guard, the exchange's 400 withdrawals collapse into
a single cluster asserting that 400 unrelated people are one entity --- a claim
that is not merely wrong but *confidently* wrong, and transitive, so it poisons
everything it touches.

So a funder above :data:`SERVICE_FUNDER_DEGREE` is treated as a service and its
addresses are not linked to each other. That is the same shape as the CoinJoin
defence in :mod:`chainscope.analysis.cluster`: a structural exception, on by
default, because the failure it prevents is unbounded and the coverage it costs
is not.

**What a cluster here means, precisely.** Shared funding is evidence of shared
*origin*, which is weaker than shared control. Two addresses funded by one key
were set up by whoever held that key; they may since have been handed to
different people. The claim is capped at MEDIUM for that reason, and the
rationale says which of the two it is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "SERVICE_FUNDER_DEGREE",
    "FundingCluster",
    "FundingEvent",
    "cluster_by_funder",
]

#: A funder paying this many distinct addresses is a service, not an operator.
#:
#: Chosen to sit far above what one person plausibly sets up by hand and far
#: below what an exchange does in an afternoon. It is a threshold on a
#: continuum, so it is reported in the result rather than applied silently ---
#: an operator running a large campaign and an exchange having a quiet day meet
#: somewhere near it, and the reader should know which side a cluster sat on.
SERVICE_FUNDER_DEGREE = 50


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """The first time an address received anything."""

    address: str
    funder: str
    tx: str = ""
    block: int | None = None
    amount_raw: int = 0

    def __post_init__(self) -> None:
        if not self.address or not self.funder:
            raise ValueError("a funding event needs both an address and a funder")


@dataclass
class FundingCluster:
    """Addresses that share a funder."""

    funder: str
    addresses: set[str] = field(default_factory=set)
    events: list[FundingEvent] = field(default_factory=list)
    is_service: bool = False
    """Whether the funder looked like a service rather than an operator. A
    service cluster is *not* an assertion that its members are related --- it is
    the record of a link deliberately not drawn."""

    @property
    def size(self) -> int:
        return len(self.addresses)

    @property
    def links_members(self) -> bool:
        """Whether this cluster asserts anything about its members."""
        return not self.is_service and self.size > 1

    def summary(self) -> str:
        if self.is_service:
            return (
                f"{self.funder} funded {self.size} addresses, which is more than "
                f"any one operator plausibly sets up by hand. Treated as a "
                f"service, so its recipients are *not* linked to each other --- "
                f"an exchange funds its customers, and clustering through one "
                f"asserts that unrelated people are the same entity."
            )
        if self.size <= 1:
            return f"{self.funder} funded only {self.address_list()}; nothing to link."
        return (
            f"{self.size} addresses were first funded by {self.funder}. That is "
            f"evidence of shared *origin* --- whoever held that key set them up. "
            f"It is weaker than shared control: they may since have been handed "
            f"to different people."
        )

    def address_list(self, limit: int = 5) -> str:
        shown = sorted(self.addresses)[:limit]
        more = self.size - len(shown)
        return ", ".join(shown) + (f" and {more} more" if more > 0 else "")

    def attribution(self, chain: ChainId | None = None) -> Attribution | None:
        """A claim about shared origin, or ``None`` when there is none to make."""
        if not self.links_members:
            return None
        return Attribution(
            label=f"funded by {self.funder}",
            category=Category.SERVICE,
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            source="chainscope common-funder clustering",
            address=self.funder,
            chain=chain,
            rationale=self.summary(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "funder": self.funder,
            "size": self.size,
            "addresses": sorted(self.addresses),
            "is_service": self.is_service,
            "links_members": self.links_members,
            "summary": self.summary(),
        }


def cluster_by_funder(
    events: list[FundingEvent],
    *,
    service_degree: int = SERVICE_FUNDER_DEGREE,
    exclude: set[str] | None = None,
) -> list[FundingCluster]:
    """Group addresses by who first funded them.

    ``exclude`` names funders known to be services --- an exchange hot wallet
    from a label set, say. Knowing that in advance is strictly better than
    inferring it from degree, because a service that funded only a handful of
    addresses in the window under examination looks exactly like an operator.

    Clusters are returned for service funders too, marked and carrying no
    claim. Dropping them would hide the fact that a link was considered and
    declined, and "why is this address not in a cluster" is a question somebody
    will ask.
    """
    known_services = {a.lower() for a in (exclude or set())}
    by_funder: dict[str, list[FundingEvent]] = defaultdict(list)
    for event in events:
        by_funder[event.funder].append(event)

    clusters: list[FundingCluster] = []
    for funder, group in by_funder.items():
        addresses = {e.address for e in group}
        # Distinct addresses, not events: an operator topping one address up
        # forty times is not forty pieces of evidence.
        is_service = funder.lower() in known_services or len(addresses) > service_degree
        clusters.append(
            FundingCluster(
                funder=funder,
                addresses=addresses,
                events=sorted(group, key=lambda e: (e.block or 0, e.address)),
                is_service=is_service,
            )
        )

    # Largest real clusters first; service clusters last, since they are
    # bookkeeping rather than findings.
    clusters.sort(key=lambda c: (c.is_service, -c.size))
    return clusters


def first_funders(transfers: list[Any]) -> list[FundingEvent]:
    """Derive funding events from transfers.

    The *first* inbound transfer to each address, by block then index. Later
    ones say nothing about origin --- by then the address exists and anybody
    can pay it, which is the difference between "who set this up" and "who has
    dealt with it".
    """
    earliest: dict[str, Any] = {}
    for transfer in transfers:
        recipient = getattr(transfer, "recipient", None)
        sender = getattr(transfer, "sender", None)
        if recipient is None or sender is None:
            continue
        key = recipient.key
        if key == sender.key:
            # A self-transfer funds nothing and would make every address its
            # own funder, producing clusters of one that look like findings.
            continue
        order = (getattr(transfer, "block", 0) or 0, getattr(transfer, "index", 0) or 0)
        if key not in earliest or order < earliest[key][0]:
            earliest[key] = (order, transfer)

    return [
        FundingEvent(
            address=key,
            funder=transfer.sender.key,
            tx=transfer.tx.hash,
            block=transfer.block,
            amount_raw=transfer.amount.raw,
        )
        for key, (_, transfer) in earliest.items()
    ]
