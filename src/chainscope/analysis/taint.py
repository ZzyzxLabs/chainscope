"""Taint tracking: how much of what this address holds came from the theft.

The question every fund-flow investigation ends at, and the one this package
could not answer. Tracing *reachability* --- did money get from A to B --- is
what the graph walk does, and it is not the same question: almost everything is
reachable from almost everything after a few hops.

Three propagation rules exist, they disagree, and the disagreement is not
academic.

**Poison.** Any transaction touching tainted funds taints all of its outputs.
Simple, and it blacklists millions of addresses within thousands of blocks. It
answers "has this address ever been near stolen money", which after a few hops
is yes for nearly everyone.

**Haircut.** Taint spreads proportionally: an output gets the same fraction of
taint as the inputs carried. Intuitively fair, and it dilutes. Measured against
132 publicised Bitcoin heists, **over 75% of all accounts with a non-zero
balance end up carrying some taint** --- a number so large it stops
distinguishing anything.

**FIFO.** The first value in funds the first value out. Measured on the same
132 heists, taint reaches **under 28% of accounts**. It is the rule this module
uses.

Three reasons, in order of weight:

*It does not diffuse.* Haircut turns one stolen coin into a millionth of a coin
of taint across a million addresses, all of them nominally tainted. FIFO keeps
the stolen value identifiable as a quantity that moved somewhere specific.

*It is reversible.* FIFO loses no information, so the same structure traces
forward from a theft and backward from a suspect balance. Haircut cannot be run
backwards; the proportions do not invert.

*It has standing.* FIFO is not an invention of blockchain analysis. English law
has tracked money through mixed accounts this way since **Clayton's Case
(1816)**, decided over a collapsed bank's ledger. That matters when a
conclusion has to survive somewhere other than a report.

Poison and haircut are implemented too, and not for completeness: the
validation harness runs all three over one graph, and the sharpest difference
turned out not to be how many addresses each paints but whether the stolen
amount survives the trip. On 100 ETH stolen:

    fifo      100.0 ETH claimed as tainted   exact
    haircut    86.4 ETH                      13.6 lost to rounding, unrecoverable
    poison    320.0 ETH                      220 manufactured, over 5x the addresses

Haircut *loses* value: proportional splitting rounds down at every hop and the
lost taint reappears nowhere, which is the concrete form of "cannot be run
backwards". Poison *invents* it: clean money arriving at a touched address
becomes stolen money by arithmetic. Only FIFO ends with the amount it started
with, which is what lets a number from it be quoted.

A choice nobody can check is a preference.

References
----------
Anderson, Shumailov, Ahmed & Rietmann, *Probing the Mystery of Cryptocurrency
Theft: An Investigation into Methods for Taint Analysis* (arXiv:1906.05754).
FIFO reference implementation: ``TaintChain/RustyTaintChain``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import partial
from typing import Any, Union

from ..core.chainid import ChainId
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context, history_of


def _history(
    provider: Any,
    *,
    chain: ChainId,
    address: str,
    lo: int,
    hi: int | str,
    cap: int,
) -> list[Any]:
    """A named function rather than a lambda closing over the loop variable,
    which is the classic way to fetch the same address n times."""
    rows: list[Any] = provider.address_history(
        chain, address, start_block=lo, end_block=hi, limit=cap
    )
    return rows


__all__ = ["Key", "Lot", "TaintAnalyzer", "TaintPolicy", "TaintResult", "trace_taint"]

#: How holdings are identified. A bare address means the chain's native asset;
#: ``(address, asset)`` names a token. Both accepted because most callers have
#: one asset and should not have to say so, and the ones that do must be able
#: to --- an ETH lot funding a USDC payment reported 1,000 clean USDC as
#: stolen before this distinction existed.
Key = Union[str, "tuple[str, str]"]


class TaintPolicy(str, Enum):
    """How taint crosses a transfer."""

    FIFO = "fifo"
    """First value in funds the first value out. Clayton's Case (1816)."""

    HAIRCUT = "haircut"
    """Proportional. Dilutes until it means nothing; kept for comparison."""

    POISON = "poison"
    """Any contact taints everything. Kept to show what it costs."""


@dataclass
class Lot:
    """A parcel of value sitting in an address, tainted or not.

    A balance is a *queue* of these rather than a number, which is the whole
    mechanism: FIFO needs to know which value arrived first, and a single
    integer cannot say.
    """

    amount: int
    tainted: int
    """How much of ``amount`` is tainted. Equal to ``amount`` or zero under
    FIFO and poison; anywhere between under haircut."""


@dataclass
class TaintResult:
    """Where the tainted value ended up."""

    policy: TaintPolicy
    tainted: dict[str, int] = field(default_factory=dict)
    """Address to tainted value currently held. Only non-zero entries."""

    touched: set[str] = field(default_factory=set)
    """Every address any taint passed through, held or not.

    Separate from :attr:`tainted` on purpose. "Currently holds stolen value" and
    "stolen value once passed through here" are different claims, and reporting
    the second as the first is how a payment processor ends up described as a
    launderer.
    """

    by_asset: dict[tuple[str, str], int] = field(default_factory=dict)
    """Tainted value per (address, asset). ``tainted`` sums these for a headline
    count, and that sum mixes units --- an address holding tainted ETH and
    tainted USDC has a total that is a number of base units, not a value. Use
    this when the split matters, which is whenever a figure is quoted."""

    unresolved: list[str] = field(default_factory=list)
    """Transfers that could not be applied because the sender's holdings were
    unknown --- history that starts mid-stream. Reported rather than treated as
    clean, since assuming clean is a claim about money nobody watched arrive."""

    @property
    def total(self) -> int:
        return sum(self.tainted.values())

    def share(self, address: str, balance: int) -> float:
        """Fraction of ``balance`` that is tainted, 0.0--1.0."""
        if balance <= 0:
            return 0.0
        return min(1.0, self.tainted.get(address.lower(), 0) / balance)

    def summary(self) -> str:
        return (
            f"{self.policy.value}: {len(self.tainted)} addresses hold tainted "
            f"value, {len(self.touched)} were touched at some point. Holding and "
            f"having-touched are different claims and only the first says "
            f"anything about a balance now."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "holding": len(self.tainted),
            "touched": len(self.touched),
            "total_tainted": str(self.total),
            "unresolved": len(self.unresolved),
            # Strings: these are wei-scale and exceed what JSON consumers parse
            # into an exact integer.
            "addresses": {a: str(v) for a, v in sorted(self.tainted.items())},
        }


def trace_taint(
    transfers: list[Any],
    sources: Mapping[Key, int] | set[Key],
    *,
    policy: TaintPolicy = TaintPolicy.FIFO,
    opening_balances: Mapping[Key, int] | None = None,
) -> TaintResult:
    """Propagate taint from ``sources`` through ``transfers`` in order.

    ``sources`` maps an address to the tainted amount it starts with, or is a
    set of addresses whose entire holdings are tainted.

    ``opening_balances`` gives each address whatever clean value it held before
    the window. Without it every address starts empty, so the first outgoing
    transfer from an address whose funding predates the window has nothing to
    draw from --- that transfer is recorded in ``unresolved`` rather than
    assumed clean, because assuming clean is a claim about money nobody watched
    arrive.

    Transfers are applied in block order. Order is the entire content of FIFO:
    applying the same set in a different sequence gives a different answer, and
    that is a property of the rule rather than a bug in it.
    """
    # Keyed by (address, asset), not by address. An address holding ETH and
    # USDC has two independent queues, and mixing them lets a clean USDC
    # payment draw taint from a dirty ETH lot --- measured: 1,000 USDC of clean
    # money reported as stolen. Raw amounts are not comparable across assets,
    # so a single queue is arithmetic on mismatched units.
    holdings: dict[tuple[str, str], deque[Lot]] = {}

    def bucket(address: str, asset: str) -> deque[Lot]:
        return holdings.setdefault((address.lower(), asset), deque())

    # Opening balances are the native asset unless said otherwise; a caller
    # with token balances passes a (address, asset) mapping.
    for key, opening in (opening_balances or {}).items():
        address, asset = key if isinstance(key, tuple) else (key, "")
        if opening > 0:
            bucket(address, asset).append(Lot(opening, 0))

    seeded = sources if isinstance(sources, dict) else dict.fromkeys(sources, 0)
    result = TaintResult(policy=policy)
    for seed_key, seed_amount in seeded.items():
        address, asset = seed_key if isinstance(seed_key, tuple) else (seed_key, "")
        result.touched.add(address.lower())
        if seed_amount > 0:
            bucket(address, asset).append(Lot(seed_amount, seed_amount))

    whole_balance = {
        (a[0].lower() if isinstance(a, tuple) else a.lower())
        for a in (sources if isinstance(sources, set) else ())
    }

    ordered = sorted(
        transfers,
        key=lambda t: (getattr(t, "block", 0) or 0, getattr(t, "index", 0) or 0),
    )
    for transfer in ordered:
        sender = getattr(transfer, "sender", None)
        recipient = getattr(transfer, "recipient", None)
        amount = getattr(transfer, "amount", None)
        if sender is None or recipient is None or amount is None or amount.raw <= 0:
            continue

        src, dst = sender.key.lower(), recipient.key.lower()
        asset_obj = getattr(transfer, "asset", None)
        asset = asset_obj.key if asset_obj else ""
        queue = bucket(src, asset)

        # An address named as a source with no explicit amount taints whatever
        # it sends, however it was funded.
        if src in whole_balance:
            _receive(bucket(dst, asset), amount.raw, amount.raw, policy)
            result.touched.add(dst)
            continue

        available = sum(lot.amount for lot in queue)
        if available < amount.raw:
            # Sent more than we watched arrive. Spend what we *did* watch and
            # treat only the shortfall as unknown.
            #
            # Dropping the whole transfer instead lost the trail completely: an
            # address that received ten tainted ETH and paid out eleven --- an
            # ordinary situation, since it had a balance before the window ---
            # passed zero taint downstream and kept all ten forever. The answer
            # became "the money stopped here", which is not merely incomplete
            # but the opposite of what happened.
            #
            # The shortfall stays uncounted rather than assumed clean, and the
            # transfer is recorded in `unresolved` either way.
            result.unresolved.append(getattr(getattr(transfer, "tx", None), "hash", "") or dst)
            moved_taint = _spend(queue, available, policy) if available else 0
            _receive(bucket(dst, asset), amount.raw, moved_taint, policy)
            if moved_taint > 0:
                result.touched.add(src)
                result.touched.add(dst)
            continue

        moved_taint = _spend(queue, amount.raw, policy)
        _receive(bucket(dst, asset), amount.raw, moved_taint, policy)
        if moved_taint > 0:
            result.touched.add(src)
            result.touched.add(dst)

    for (address, _asset), queue in holdings.items():
        held = sum(lot.tainted for lot in queue)
        if held > 0:
            # Summed across assets for the headline figure. Raw units differ,
            # so this is a count of tainted base units and not a value ---
            # `by_asset` keeps them apart for anything that needs the split.
            result.tainted[address] = result.tainted.get(address, 0) + held
    for (address, asset), queue in holdings.items():
        held = sum(lot.tainted for lot in queue)
        if held > 0:
            result.by_asset[(address, asset)] = held
    return result


def _spend(queue: deque[Lot], amount: int, policy: TaintPolicy) -> int:
    """Remove ``amount`` from ``queue`` and return how much of it was tainted."""
    if policy is TaintPolicy.HAIRCUT:
        total = sum(lot.amount for lot in queue)
        tainted = sum(lot.tainted for lot in queue)
        if total <= 0:
            return 0
        # Proportional, and this line is where haircut loses. The taint splits
        # across every output forever and never resolves back to a quantity.
        moved = amount * tainted // total
        remaining = amount
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(lot.amount, remaining)
            share = lot.tainted * take // lot.amount if lot.amount else 0
            lot.amount -= take
            lot.tainted -= share
            remaining -= take
            if lot.amount <= 0:
                queue.popleft()
        return moved

    if policy is TaintPolicy.POISON:
        tainted_here = any(lot.tainted > 0 for lot in queue)
        remaining = amount
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(lot.amount, remaining)
            lot.amount -= take
            lot.tainted = max(0, lot.tainted - take)
            remaining -= take
            if lot.amount <= 0:
                queue.popleft()
        # Everything leaving a touched address is fully tainted, which is why
        # this blacklists exponentially.
        return amount if tainted_here else 0

    # FIFO: draw from the front, and the taint of what is drawn is the taint
    # that moves. No proportion is computed and nothing is diluted.
    moved = 0
    remaining = amount
    while remaining > 0 and queue:
        lot = queue[0]
        take = min(lot.amount, remaining)
        share = min(lot.tainted, take)
        lot.amount -= take
        lot.tainted -= share
        moved += share
        remaining -= take
        if lot.amount <= 0:
            queue.popleft()
    return moved


def _receive(queue: deque[Lot], amount: int, tainted: int, policy: TaintPolicy) -> None:
    if policy is TaintPolicy.POISON:
        already = any(lot.tainted > 0 for lot in queue)
        if tainted > 0:
            # Retroactive: everything already sitting here becomes tainted.
            for lot in queue:
                lot.tainted = lot.amount
        if tainted > 0 or already:
            # And clean money arriving at a tainted address is tainted on
            # arrival. This is what "any contact" means, and it is why the
            # rule reports more tainted value than was ever stolen.
            queue.append(Lot(amount, amount))
            return
    queue.append(Lot(amount, min(tainted, amount)))


def trace_origins(
    transfers: list[Any],
    target: Key,
    *,
    amount: int | None = None,
) -> dict[Key, int]:
    """Where the value an address holds came from --- FIFO run backwards.

    The other half of the question, and the reason FIFO was chosen over
    haircut. Forward tracing asks "where did the theft go"; this asks "what
    funded this balance", which is what an investigator has when they start
    from a suspect rather than from an incident.

    It works because **FIFO loses no information**. Each lot in an address's
    queue remembers which incoming transfer put it there, so the queue can be
    read in either direction. Haircut cannot: proportional splitting mixes
    every source into every output and the proportions do not invert --- you
    can say a balance is 3% tainted and never which 3%.

    Returns each origin and how much of ``target``'s current holding came from
    it, in base units of that holding's asset. ``amount`` limits the tracing to
    the most recent ``amount`` units, which is how you ask "where did the last
    ten ETH come from" rather than "where did everything come from".

    Origins are *immediate* senders, not ultimate ones. Following further is
    the caller's decision, because each hop back multiplies the addresses under
    examination and an unbounded walk would return the chain's history.
    """
    address, asset = target if isinstance(target, tuple) else (target, "")
    address = address.lower()

    # Replay forward, tracking provenance per lot. A lot's origin is whoever
    # sent it; a lot spent onward carries its origin with it, which is what
    # makes the replay reversible in the first place.
    queues: dict[tuple[str, str], deque[tuple[int, Key]]] = {}

    ordered = sorted(
        transfers,
        key=lambda t: (getattr(t, "block", 0) or 0, getattr(t, "index", 0) or 0),
    )
    for transfer in ordered:
        sender = getattr(transfer, "sender", None)
        recipient = getattr(transfer, "recipient", None)
        value = getattr(transfer, "amount", None)
        if sender is None or recipient is None or value is None or value.raw <= 0:
            continue
        asset_obj = getattr(transfer, "asset", None)
        key = asset_obj.key if asset_obj else ""
        src = queues.setdefault((sender.key.lower(), key), deque())

        # Draw from the front, carrying each lot's origin forward. A shortfall
        # is credited to the sender itself: the value existed before the window
        # and the sender is the furthest back this data reaches.
        remaining = value.raw
        carried: list[tuple[int, Key]] = []
        while remaining > 0 and src:
            size, origin = src[0]
            take = min(size, remaining)
            carried.append((take, origin))
            remaining -= take
            if take == size:
                src.popleft()
            else:
                src[0] = (size - take, origin)
        if remaining > 0:
            carried.append((remaining, sender.key.lower()))

        dst = queues.setdefault((recipient.key.lower(), key), deque())
        dst.extend(carried)

    held = queues.get((address, asset), deque())
    # Most recent first: "where did the last ten ETH come from" reads the back
    # of the queue, since FIFO spends the front.
    wanted = amount if amount is not None else sum(size for size, _ in held)
    origins: dict[Key, int] = {}
    for size, origin in reversed(held):
        if wanted <= 0:
            break
        take = min(size, wanted)
        origins[origin] = origins.get(origin, 0) + take
        wanted -= take
    return origins


class TaintAnalyzer(Analyzer):
    """Trace stolen value forward from a source address."""

    name = "taint"
    version = "1.0"
    description = "Trace how much of each address's holdings came from a given source"

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, Capability.ADDRESS_HISTORY))

    def run(
        self,
        ctx: Context,
        *,
        source: str = "",
        amount: str = "",
        policy: str = TaintPolicy.FIFO.value,
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not source:
            raise ValueError("taint tracing needs a `source` address to trace from")
        try:
            rule = TaintPolicy(policy)
        except ValueError as exc:
            raise ValueError(
                f"policy must be one of {', '.join(p.value for p in TaintPolicy)}"
            ) from exc

        seed = source.lower()
        per_node = ctx.limit("per_node", 1000)
        max_nodes = ctx.limit("max_nodes", 60)

        # Follow the money, not just the first hop.
        #
        # Fetching only the source's history makes multi-hop tracing
        # impossible: `source -> A -> B` contains the first transfer and
        # nothing else, so B can never be reported as holding anything. The
        # answer would be a confident subset --- the shape this package exists
        # to refuse --- and it would look identical to a genuinely short chain.
        #
        # Breadth-first over addresses that received tainted value, capped, and
        # the cap is reported rather than absorbed.
        warnings: list[str] = []
        seen: set[str] = set()
        frontier = [seed]
        transfers: list[Any] = []
        capped = False

        while frontier and len(seen) < max_nodes:
            address = frontier.pop(0)
            if address in seen:
                continue
            seen.add(address)
            try:
                history, source_notes = history_of(
                    ctx,
                    partial(
                        _history,
                        chain=ctx.chain,
                        address=address,
                        lo=start_block,
                        hi=end_block,
                        cap=per_node,
                    ),
                )
                # FIFO depends on arrival order, so a source that came back
                # short does not merely lose a row --- it changes which lot paid
                # for what, everywhere downstream of this address.
                for note in source_notes:
                    warnings.append(f"{address}: {note}")
            except Exception as exc:
                # Named, not swallowed. An address whose history could not be
                # fetched is a hole in the trace, and treating it as a dead end
                # would report the money as stopping there.
                warnings.append(f"could not fetch history for {address}: {exc}")
                continue

            if len(history) >= per_node:
                warnings.append(
                    f"{address} filled the {per_node}-row limit. FIFO depends on "
                    f"the order value arrived in, so a clipped window does not "
                    f"just lose hops: it changes which funds paid for what."
                )
            moved = [t for tx in history for t in tx.value_transfers()]
            transfers.extend(moved)
            for t in moved:
                if t.sender and t.sender.key.lower() == address and t.recipient:
                    nxt = t.recipient.key.lower()
                    if nxt not in seen:
                        if len(seen) + len(frontier) >= max_nodes:
                            capped = True
                        else:
                            frontier.append(nxt)

        if capped or frontier:
            warnings.append(
                f"the walk stopped at {max_nodes} addresses. Value beyond them is "
                f"not traced and is not reported as settled --- raise max_nodes to "
                f"follow further."
            )
        if rule is not TaintPolicy.FIFO:
            warnings.append(
                f"policy={rule.value} is implemented for comparison, not for use. "
                f"Measured on one graph from 100 ETH stolen: haircut reports 86.4 "
                f"(losing value it cannot recover) and poison 320.0 (inventing "
                f"value never stolen). Only FIFO conserves the amount."
            )

        sources: Mapping[Key, int] | set[Key] = {seed: int(amount)} if amount else {seed}
        result = trace_taint(transfers, sources, policy=rule)

        findings = [
            Finding(
                title=f"{address} holds tainted value",
                severity=Severity.NOTABLE,
                detail=(
                    f"{held} raw units traceable to {seed} under {rule.value}. "
                    f"This is value held, not value that merely passed through."
                ),
                data={"address": address, "tainted_raw": str(held), "policy": rule.value},
            )
            for address, held in sorted(result.tainted.items(), key=lambda kv: -kv[1])
        ]
        if result.unresolved:
            warnings.append(
                f"{len(result.unresolved)} transfers spent from holdings that "
                f"arrived before this window. They are not counted as clean --- "
                f"clean is a claim about money nobody watched arrive."
            )
        passed_through = len(result.touched) - len(result.tainted)
        if passed_through > 0:
            warnings.append(
                f"{passed_through} further addresses were touched but hold none "
                f"now. Reporting those as holders is how a payment processor "
                f"gets described as a launderer."
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={
                "source": seed,
                "policy": rule.value,
                "amount": amount,
                "start_block": start_block,
                "end_block": end_block,
                "per_node": per_node,
                "max_nodes": max_nodes,
                # Which addresses the walk actually reached. Without it a rerun
                # against a different provider silently covers different ground
                # and the two results are not comparable.
                "addresses_walked": sorted(seen),
            },
            started=started,
        )
