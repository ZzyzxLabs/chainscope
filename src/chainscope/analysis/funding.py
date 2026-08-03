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
from datetime import datetime, timezone
from functools import partial
from typing import Any, ClassVar

from ..chains import fold_if_hex
from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..core.models import Transaction
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability, Provider
from .base import Analyzer, Context, history_of

__all__ = [
    "RELAY_RESIDUE_BPS",
    "SERVICE_FUNDER_DEGREE",
    "CommonFunderAnalyzer",
    "FundingCluster",
    "FundingEvent",
    "PassThrough",
    "cluster_by_funder",
    "find_pass_throughs",
    "first_funders",
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

    # Grouped on the lowercased address, and addresses deduplicated the same
    # way. Grouping on the raw string split one funder written in two cases into
    # two clusters of one --- and a cluster of one asserts nothing, so the
    # technique returned "no shared funding" rather than failing. A false
    # negative that reads as a finding.
    #
    # The internal pipeline happened to be safe (`Address.key` is already
    # lowercase), which is precisely why this survived: the validation harness
    # generated every funder in one case, so no amount of measured precision
    # could have exposed it.
    by_funder: dict[str, list[FundingEvent]] = defaultdict(list)
    display: dict[str, str] = {}
    for event in events:
        key = event.funder.lower()
        by_funder[key].append(event)
        # First spelling seen wins, so the output shows a checksummed address
        # the way the user pasted it rather than a flattened one.
        display.setdefault(key, event.funder)

    clusters: list[FundingCluster] = []
    for key, group in by_funder.items():
        addresses = {fold_if_hex(e.address) for e in group}
        # Distinct addresses, not events: an operator topping one address up
        # forty times is not forty pieces of evidence.
        is_service = key in known_services or len(addresses) > service_degree
        clusters.append(
            FundingCluster(
                funder=display[key],
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


def _history(
    provider: Provider,
    *,
    chain: ChainId,
    address: str,
    lo: int,
    hi: int | str,
    cap: int,
) -> list[Transaction]:
    """A named function rather than a lambda in the loop.

    A lambda closing over the loop variable is the classic way to fetch the same
    address ``n`` times, and mypy cannot infer the type of the default-argument
    workaround either.
    """
    return provider.address_history(chain, address, start_block=lo, end_block=hi, limit=cap)


class CommonFunderAnalyzer(Analyzer):
    """Group addresses by who first funded them."""

    #: Parameters this needs beyond an address. Read by the web UI to
    #: render an input for each, so a reader is not asked to press a button
    #: whose only possible outcome is an error naming what they should have
    #: typed. Kept beside the check that enforces it.
    REQUIRES: ClassVar[tuple[str, ...]] = ("addresses",)

    name = "common-funder"
    version = "1.0"
    description = "Group account-model addresses by who first funded them"

    def applicable(self, ctx: Context) -> bool:
        # Needs to enumerate each address's inbound history to find its *first*
        # funder. Plain RPC cannot do that.
        return bool(ctx.router.candidates(ctx.chain, Capability.ADDRESS_HISTORY))

    def run(
        self,
        ctx: Context,
        *,
        addresses: str = "",
        service_degree: int = SERVICE_FUNDER_DEGREE,
        exclude: str = "",
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        seeds = [a.strip().lower() for a in addresses.split(",") if a.strip()]
        if not seeds:
            raise ValueError(
                "common-funder clustering needs `addresses` --- a comma-separated "
                "list of the addresses to group"
            )

        per_node = ctx.limit("per_node", 500)
        warnings: list[str] = []
        events: list[FundingEvent] = []

        for seed in seeds:
            fetch = partial(
                _history,
                chain=ctx.chain,
                address=seed,
                lo=start_block,
                hi=end_block,
                cap=per_node,
            )
            try:
                history, completeness = history_of(ctx, fetch)
                warnings.extend(completeness)
            except Exception as exc:
                # Named, not swallowed. An address whose history could not be
                # fetched has no funder here, and a cluster that silently omits
                # it looks like a cluster that considered and excluded it.
                warnings.append(f"could not fetch history for {seed}: {exc}")
                continue

            if len(history) >= per_node:
                # The first funder is the *earliest* inbound transfer, and a
                # capped page is not guaranteed to reach back that far. Without
                # this the cluster still forms, and it is a cluster around
                # whoever happened to pay first inside the window.
                warnings.append(
                    f"{seed} returned the full page of {per_node} transactions, so "
                    f"its earliest inbound transfer may lie outside the window and "
                    f"its apparent funder may not be its first"
                )

            # Failed transactions and zero-value calls are dropped here; a
            # reverted transfer funds nothing, however it looks in a history.
            inbound = [
                t
                for tx in history
                for t in tx.value_transfers()
                if t.recipient and t.recipient.key.lower() == seed
            ]
            events.extend(first_funders(inbound))

        if not events:
            return self._result(
                ctx,
                warnings=(*warnings, "no funding events found for any of the given addresses"),
                params={
                    "addresses": seeds,
                    "service_degree": service_degree,
                    "exclude": sorted(
                        {a.strip().lower() for a in exclude.split(",") if a.strip()}
                    ),
                    "start_block": start_block,
                    "end_block": end_block,
                    "per_node": per_node,
                },
                started=started,
            )

        excluded = {a.strip().lower() for a in exclude.split(",") if a.strip()}
        clusters = cluster_by_funder(events, service_degree=service_degree, exclude=excluded)

        findings = [
            Finding(
                title=(
                    f"{c.size} addresses share the funder {c.funder}"
                    if c.links_members
                    else f"{c.funder} looks like a service ({c.size} addresses)"
                ),
                # A service cluster is a link declined, not a link found.
                severity=Severity.NOTABLE if c.links_members else Severity.INFO,
                detail=c.summary(),
                data=c.to_dict(),
            )
            for c in clusters
            if c.size > 1
        ]

        unfunded = len(seeds) - len({e.address for e in events})
        if unfunded > 0:
            warnings.append(
                f"{unfunded} of {len(seeds)} addresses had no inbound transfer in "
                f"the window and appear in no cluster"
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={
                "addresses": seeds,
                "service_degree": service_degree,
                "exclude": sorted(excluded),
                "start_block": start_block,
                "end_block": end_block,
                "per_node": per_node,
            },
            started=started,
        )


# ------------------------------------------------------------------ relays

#: How much of what arrived may remain before an address stops looking like a
#: pass-through, in hundredths of a percent.
#:
#: Not zero. A relay pays gas out of the same balance, and on a token transfer
#: it may leave dust behind rather than compute an exact sweep. Requiring an
#: exact zero would miss most real ones; allowing much more admits ordinary
#: wallets that happen to be nearly empty.
RELAY_RESIDUE_BPS = 100


@dataclass(frozen=True, slots=True)
class PassThrough:
    """An address that received once, sent it onward, and stopped."""

    address: str
    funder: str
    payee: str
    received: int
    sent: int
    decimals: int
    symbol: str

    @property
    def residue(self) -> int:
        return max(0, self.received - self.sent)

    @property
    def residue_bps(self) -> int:
        return (self.residue * 10_000) // self.received if self.received else 0

    def summary(self) -> str:
        return (
            f"{self.address} received once from {self.funder} and sent onward to "
            f"{self.payee}, keeping {self.residue_bps / 100:.2f}% of it. An address "
            f"created for one hop: it holds nothing, has no history before or "
            f"after, and exists to put a step between two parties. It says the "
            f"step was deliberate --- and nothing about who took it."
        )

    def attribution(self, chain: ChainId | None = None) -> Attribution:
        return Attribution(
            label=f"one-hop relay between {self.funder} and {self.payee}",
            category=Category.SERVICE,
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            source="chainscope pass-through detection",
            address=self.address,
            chain=chain,
            rationale=self.summary(),
        )


def find_pass_throughs(
    transfers: list[Any], *, max_residue_bps: int = RELAY_RESIDUE_BPS
) -> list[PassThrough]:
    """Addresses whose entire history is one transfer in and one out.

    The shape recorded across several traces: twelve one-time deposit addresses
    into one exchange, twenty-one into a secondary relay. An operator creates an
    address, funds it, sweeps it, and abandons it --- which is cheap to do and
    leaves a signature that is hard to avoid.

    **Exactly two transfers, not "few".** Three is a different thing: an address
    that received twice was reused, and reuse is the property being tested for.
    The rule is brittle on purpose, because a loose version admits every quiet
    wallet in the data.

    A residue allowance exists because a relay pays gas from the same balance
    and may leave dust rather than compute an exact sweep. Requiring zero would
    miss most real ones.

    Says the hop was deliberate. Says nothing about who made it: an exchange
    generating a deposit address per customer produces exactly this shape, which
    is why the claim caps at MEDIUM and names the alternative.
    """
    history: dict[tuple[str, str], list[Any]] = {}
    for t in transfers:
        sender = getattr(t, "sender", None)
        recipient = getattr(t, "recipient", None)
        amount = getattr(t, "amount", None)
        if sender is None or recipient is None or amount is None or amount.raw <= 0:
            continue
        asset_obj = getattr(t, "asset", None)
        asset = asset_obj.key if asset_obj else ""
        for side in (sender.key.lower(), recipient.key.lower()):
            history.setdefault((side, asset), []).append(t)

    found: list[PassThrough] = []
    for (address, _asset), rows in history.items():
        if len(rows) != 2:
            continue
        rows.sort(key=lambda t: (getattr(t, "block", 0) or 0, getattr(t, "index", 0) or 0))
        inbound, outbound = rows
        if not (inbound.recipient and inbound.recipient.key.lower() == address):
            continue
        if not (outbound.sender and outbound.sender.key.lower() == address):
            continue
        # Order matters: sending before receiving is a different address with a
        # balance we never saw arrive, not a relay.
        if outbound.amount.raw > inbound.amount.raw:
            continue
        relay = PassThrough(
            address=address,
            funder=inbound.sender.key if inbound.sender else "",
            payee=outbound.recipient.key if outbound.recipient else "",
            received=inbound.amount.raw,
            sent=outbound.amount.raw,
            decimals=inbound.amount.decimals,
            symbol=inbound.amount.symbol,
        )
        if relay.residue_bps <= max_residue_bps:
            found.append(relay)

    found.sort(key=lambda r: -r.received)
    return found
