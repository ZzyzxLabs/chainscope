"""Watches: pure functions of a block range. No scheduler, no clock.

Monitoring is a legitimate thing to want and a poor thing to own. A scheduler
means a process, which means uptime, restarts, at-least-once delivery, and a
persistent notion of "now" --- and every one of those makes the analysis layer
harder to test and impossible to replay.

So this module provides evaluation and nothing else. Who calls it is not its
concern: cron, a systemd timer, a CI workflow, a user's own daemon, a
``while true`` loop. Freshness becomes the operator's dial rather than an
architectural constant.

Three properties fall out, and the third is why this shape was chosen.

1. No process to run, no uptime to promise, and no clock in the test suite.
2. Delivery is the caller's problem, so this cannot be wrong about it.
3. **Alerts are replayable.** Evaluation over a fixed block range is
   deterministic, so *"why did this fire?"* is answered by running it again
   against the bundle, on the same recorded responses that triggered it. An
   alerting system that cannot reconstruct its own past decisions is not usable
   as evidence, and most cannot.

Predicates are values rather than lambdas for the same reason
:class:`~chainscope.store.base.Query` is: a watch that cannot be serialised
cannot travel in a case bundle, and a rule nobody can read six months later is
a rule nobody can defend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from ..core.attribution import Category, Confidence
from ..core.chainid import ChainId
from ..core.models import Address, Transfer

if TYPE_CHECKING:  # pragma: no cover
    from ..store.base import Store

__all__ = [
    "AmountOver",
    "AnyOf",
    "CounterpartyIn",
    "CounterpartyIsUnknown",
    "Event",
    "Predicate",
    "Severity",
    "TouchesCategory",
    "Watch",
    "WatchError",
    "evaluate",
]


class WatchError(RuntimeError):
    """A watch could not be evaluated."""


class EvaluationIncomplete(WatchError):
    """The range held more transfers than could be examined.

    A distinct type because the caller must not treat this as "no further
    matches". An alerting system that quietly stops reading is worse than one
    that fails: the events it did produce look like the complete answer, and
    the miss is invisible until somebody asks why nothing fired.
    """


#: Transfers examined per evaluation before giving up. High enough that no
#: ordinary subject reaches it, low enough that a runaway query does not exhaust
#: memory. Reaching it raises rather than truncating.
MAX_TRANSFERS = 1_000_000


class Severity(str, Enum):
    """How much a match should interrupt somebody.

    Distinct from :class:`~chainscope.core.attribution.Confidence`, and
    conflating the two is a mistake worth naming: confidence is how sure we are
    the claim is true, severity is how much it matters if it is. A CERTAIN
    label on a known exchange deposit is not an alert; a MEDIUM one on a
    sanctioned counterparty is.
    """

    INFO = "info"
    NOTABLE = "notable"
    URGENT = "urgent"


# --------------------------------------------------------------------- context


class Context(Protocol):
    """What a predicate is allowed to look at.

    Narrow on purpose. A predicate that can reach the network is a predicate
    whose result depends on when it ran, which breaks the replay guarantee that
    is the whole point of this design.
    """

    subject: str
    """Whose watch this is. Predicates need it to answer "the *other* side",
    which is not a fixed side of the transfer: on an inbound transfer the
    subject is the recipient, and a rule that always inspects the recipient
    would be examining the subject rather than its counterparty."""

    def attributions(self, address: str) -> list[Any]: ...


@dataclass
class StoreContext:
    """A :class:`Context` backed by a store, with lookups memoised.

    Memoising matters more than it looks: a watch over a thousand transfers
    from one address would otherwise ask about the same counterparty a thousand
    times, and each is a query.
    """

    store: Store
    subject: str = ""
    _cache: dict[str, list[Any]] = field(default_factory=dict, repr=False)

    def attributions(self, address: str) -> list[Any]:
        key = address.lower()
        if key not in self._cache:
            self._cache[key] = self.store.attributions(address)
        return self._cache[key]

    def counterparty(self, transfer: Transfer) -> Address | None:
        """The side of ``transfer`` that is not the subject.

        Falls back to the recipient when the subject appears on neither side,
        which happens for a watch whose subject is a cluster member rather than
        the literal address. Guessing is better than returning nothing there:
        the alternative is a rule that silently never fires.
        """
        key = self.subject.lower()
        if transfer.sender is not None and transfer.sender.key.lower() == key:
            return transfer.recipient
        if transfer.recipient is not None and transfer.recipient.key.lower() == key:
            return transfer.sender
        return transfer.recipient


# ------------------------------------------------------------------ predicates


class Predicate(Protocol):
    """A serialisable test over one transfer."""

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        """Return a human-readable reason if it matches, else ``None``.

        Returning the reason rather than a bare ``True`` is deliberate. An
        alert that says only "watch 'exchange-outflow' fired" sends the reader
        back to the data to work out why; one that says "8,116 ETH to an
        address labelled eXch (HIGH, explorer nametag)" does not.
        """

    def describe(self) -> str: ...

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AmountOver:
    """Fires on a single transfer above a threshold.

    ``threshold`` is raw integer units --- wei, satoshi, MIST. Not a decimal
    string and never a float: a threshold of "10 ETH" written as 10.0 and
    multiplied out is already the wrong number.
    """

    threshold: int
    symbol: str = ""
    """Restrict to one asset. Empty means any --- which is usually wrong, since
    a threshold meaningful for ETH is meaningless for USDC."""

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        if self.symbol and transfer.amount.symbol != self.symbol:
            return None
        if transfer.amount.raw <= self.threshold:
            return None
        return f"{transfer.amount} exceeds threshold"

    def describe(self) -> str:
        asset = f" of {self.symbol}" if self.symbol else ""
        return f"amount over {self.threshold} raw units{asset}"

    def to_dict(self) -> dict[str, Any]:
        # The threshold is a string for the same reason amounts are everywhere
        # else here: it can exceed what JSON holds as a number.
        return {"kind": "amount_over", "threshold": str(self.threshold), "symbol": self.symbol}


@dataclass(frozen=True, slots=True)
class TouchesCategory:
    """Fires when either side of a transfer carries a given category.

    ``min_confidence`` exists because the useful setting is usually not
    "certain". Waiting for certainty on a sanctions match means not alerting on
    the case where it matters, so the default admits MEDIUM and the event says
    what confidence it fired on.
    """

    category: Category
    min_confidence: Confidence = Confidence.MEDIUM

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        for side, address in (("sender", transfer.sender), ("recipient", transfer.recipient)):
            if address is None:
                continue
            for claim in ctx.attributions(address.key):
                if claim.category is self.category and claim.confidence >= self.min_confidence:
                    return (
                        f"{side} {address.key} is {self.category.value}: "
                        f"{claim.label} ({claim.confidence.name}, {claim.source})"
                    )
        return None

    def describe(self) -> str:
        return f"touches {self.category.value} at {self.min_confidence.name} or better"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "touches_category",
            "category": self.category.value,
            "min_confidence": int(self.min_confidence),
        }


@dataclass(frozen=True, slots=True)
class CounterpartyIn:
    """Fires when the other side is one of a named set."""

    addresses: frozenset[str]
    label: str = ""

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        # The counterparty, not either side. A subject that appears in its own
        # watched set --- an exchange's own address in a list of exchanges, say
        # --- would otherwise match every transfer it is part of.
        address = _counterparty(transfer, ctx)
        if address is not None and address.key.lower() in self.addresses:
            name = f" ({self.label})" if self.label else ""
            return f"counterparty {address.key} is in the watched set{name}"
        return None

    def describe(self) -> str:
        named = f" ({self.label})" if self.label else ""
        return f"counterparty in a set of {len(self.addresses)}{named}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "counterparty_in",
            "addresses": sorted(self.addresses),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class CounterpartyIsUnknown:
    """Fires when the *other* side of a transfer has no attribution at all.

    Worth having as a first-class rule rather than a gap: funds moving to an
    address nobody has ever labelled is the ordinary shape of a new laundering
    route, and a monitoring setup built only from known-bad lists cannot see it.

    "The other side" is resolved against the watch's subject rather than being
    a fixed side of the transfer. Always inspecting the recipient would examine
    the subject itself on every inbound transfer, and since the subject is
    rarely labelled in its own store, the rule would fire on all of them.
    """

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        address = _counterparty(transfer, ctx)
        if address is None:
            return None
        if ctx.attributions(address.key):
            return None
        return f"counterparty {address.key} has no attribution from any source"

    def describe(self) -> str:
        return "counterparty is unlabelled"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "counterparty_unknown"}


@dataclass(frozen=True, slots=True)
class AnyOf:
    """Fires if any child does. Reasons are joined so nothing is lost."""

    predicates: tuple[Predicate, ...]

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        reasons = [r for p in self.predicates if (r := p.matches(transfer, ctx))]
        return "; ".join(reasons) if reasons else None

    def describe(self) -> str:
        return " OR ".join(p.describe() for p in self.predicates)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "any_of", "predicates": [p.to_dict() for p in self.predicates]}


@dataclass(frozen=True, slots=True)
class AllOf:
    """Fires only if every child does."""

    predicates: tuple[Predicate, ...]

    def matches(self, transfer: Transfer, ctx: Context) -> str | None:
        reasons = []
        for p in self.predicates:
            reason = p.matches(transfer, ctx)
            if reason is None:
                return None
            reasons.append(reason)
        return "; ".join(reasons)

    def describe(self) -> str:
        return " AND ".join(p.describe() for p in self.predicates)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "all_of", "predicates": [p.to_dict() for p in self.predicates]}


def _counterparty(transfer: Transfer, ctx: Context) -> Address | None:
    """The side of ``transfer`` that is not the context's subject."""
    resolver = getattr(ctx, "counterparty", None)
    if resolver is not None:
        result: Address | None = resolver(transfer)
        return result
    return transfer.recipient


# ---------------------------------------------------------------- watch, event


@dataclass(frozen=True, slots=True)
class Watch:
    """A named rule over one subject."""

    name: str
    subject: str
    """An address. Cluster and saved-query subjects are not implemented; a watch
    that silently matched nothing because its subject type was unsupported would
    be worse than one that refuses to be built."""

    predicate: Predicate
    chain: ChainId
    severity: Severity = Severity.NOTABLE
    direction: str = "both"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise WatchError("a watch needs a name --- it appears in every event it raises")
        if not self.subject.strip():
            raise WatchError("a watch needs a subject")
        if self.direction not in ("out", "in", "both"):
            raise WatchError(
                f"direction must be 'out', 'in', or 'both', not {self.direction!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "subject": self.subject,
            "chain": str(self.chain),
            "severity": self.severity.value,
            "direction": self.direction,
            "predicate": self.predicate.to_dict(),
            "describes": self.predicate.describe(),
        }


@dataclass(frozen=True, slots=True)
class Event:
    """One match, with everything needed to re-derive it."""

    watch: str
    subject: str
    chain: ChainId
    severity: Severity
    reason: str
    transfer: Transfer
    since: int
    until: int
    """The block range evaluated. Carrying it is what makes an event replayable:
    re-running the same watch over the same range must produce the same events,
    and without the range there is nothing to re-run."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "watch": self.watch,
            "subject": self.subject,
            "chain": str(self.chain),
            "severity": self.severity.value,
            "reason": self.reason,
            "tx": self.transfer.tx.hash,
            "block": self.transfer.block,
            "amount": str(self.transfer.amount.raw),
            "symbol": self.transfer.amount.symbol,
            "decimals": self.transfer.amount.decimals,
            "range": [self.since, self.until],
        }

    def __str__(self) -> str:
        return (
            f"[{self.severity.value}] {self.watch}: {self.reason} (tx {self.transfer.tx.hash})"
        )


# ------------------------------------------------------------------- evaluate


def evaluate(
    watch: Watch,
    store: Store,
    since: int,
    until: int,
    *,
    ctx: Context | None = None,
) -> list[Event]:
    """Every match for ``watch`` in the block range ``[since, until]``.

    Pure with respect to the store: same store, same range, same events. There
    is no clock here and no notion of "new since last time" --- the caller owns
    the cursor, because a cursor is state and state is what makes an alerting
    system unable to explain itself.

    The range is inclusive at both ends. A half-open range would be more
    conventional and would also make it easy for a caller stepping through
    history to skip exactly one block per step, which is the sort of gap nobody
    notices until an alert did not fire.
    """
    if since > until:
        raise WatchError(f"empty range: since={since} is after until={until}")

    from ..store.base import Query

    context = ctx or StoreContext(store, subject=watch.subject)
    query = Query(
        chain=watch.chain,
        address=watch.subject if watch.direction == "both" else None,
        sender=watch.subject if watch.direction == "out" else None,
        recipient=watch.subject if watch.direction == "in" else None,
        min_block=since,
        max_block=until,
        # One past the ceiling, so hitting it is a fact rather than an
        # inference from a full page.
        limit=MAX_TRANSFERS + 1,
    )

    events: list[Event] = []
    for examined, transfer in enumerate(store.transfers(query), start=1):
        if examined > MAX_TRANSFERS:
            raise EvaluationIncomplete(
                f"{watch.name!r} over blocks {since}-{until} has more than "
                f"{MAX_TRANSFERS:,} transfers for {watch.subject}. Evaluating "
                f"part of a range and reporting the result as complete is the "
                f"failure this design exists to prevent, so nothing is "
                f"returned. Narrow the block range and evaluate in steps."
            )
        reason = watch.predicate.matches(transfer, context)
        if reason is not None:
            events.append(
                Event(
                    watch=watch.name,
                    subject=watch.subject,
                    chain=watch.chain,
                    severity=watch.severity,
                    reason=reason,
                    transfer=transfer,
                    since=since,
                    until=until,
                )
            )
    return events


def evaluate_all(watches: list[Watch], store: Store, since: int, until: int) -> list[Event]:
    """Evaluate several watches over one range, sharing the lookup cache."""
    # One cache across all watches, but the subject differs per watch, so the
    # context is rebound rather than shared wholesale.
    shared: dict[str, list[Any]] = {}
    out: list[Event] = []
    for watch in watches:
        context = StoreContext(store, subject=watch.subject)
        context._cache = shared
        out.extend(evaluate(watch, store, since, until, ctx=context))
    # Most severe first: a list that buries the urgent one under forty
    # informational matches has not alerted anybody.
    order = {Severity.URGENT: 0, Severity.NOTABLE: 1, Severity.INFO: 2}
    out.sort(key=lambda e: (order[e.severity], e.transfer.block or 0))
    return out
