"""How did money get from A to B --- and did it, actually.

The question a flow-analysis tool exists to answer, and the one interaction
every commercial product in this space is built around: give it two addresses
and it draws the line between them.

Drawing a line is easy. Drawing a line that is *true* requires refusing three
tempting shortcuts, each of which produces a picture indistinguishable from a
correct one.

**A path must respect time.** The obvious implementation --- breadth-first
search over the transfer graph --- has no notion of when anything happened, so
it happily returns ``A → X → B`` where X paid B *before* A ever paid X. Money
cannot travel that way. Measured on a real ledger of 55 transfers between 37
addresses: of 224 multi-hop shortest paths BFS returned, **138 were causally
impossible --- 62%.** Not an edge case. The majority.

The fix is a *time-respecting path*, the standard object in temporal graph
theory: a walk whose edge timestamps are non-decreasing. The formulation and
the hardness results are from Kempe, Kleinberg & Kumar, *Connectivity and
Inference Problems for Temporal Networks* (STOC 2000); the efficient
earliest-arrival algorithms are surveyed in Wu et al., *Path Problems in
Temporal Graphs* (VLDB 2014). :func:`find_routes` is a bounded enumeration of
them.

**A path through a hub is not a path.** An exchange hot wallet touches
everything, so a search that may cross one finds a route between almost any two
addresses on the chain --- and that route means nothing, because funds entering
a custodian are commingled and what comes out is a different coin. The
structural link is real; the *causal* link is destroyed. Hubs are therefore
detected by degree, and a route that crosses one is returned **marked broken at
that address**, not silently dropped and not silently counted. Both of those
would be a decision the reader cannot see.

**A path cannot carry more than its narrowest hop.** A route whose second hop
moved 0.001 ETH cannot be how 1,000 ETH travelled. Every route reports its
:attr:`Route.carries` --- the minimum along it --- so "there is a path" and
"this path could have carried the amount in question" stay separate claims.

What this does **not** do is claim the money took the route it found. A
time-respecting, hub-free, amount-plausible path is a *candidate*: it is what
remains after the impossible has been removed, which is a much weaker statement
than proof and a much stronger one than a line on a picture.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..chains import address_key, fold_if_hex
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability, ProviderError
from .base import Analyzer, Context, history_of
from .impersonation import trusted_assets

__all__ = [
    "DEFAULT_HUB_DEGREE",
    "Hop",
    "Route",
    "RouteAnalyzer",
    "find_routes",
    "findings",
    "hubs_in",
]

#: Distinct counterparties past which an address is treated as a hub.
#:
#: Chosen from what the structure means rather than from a round number: an
#: address that has transacted with this many distinct parties is a service, and
#: a service commingles. The exact figure matters less than that there *is* one
#: --- with no bound, every route runs through the busiest address in the store
#: and the tool reports a connection between any two addresses ever recorded.
#:
#: Deliberately generous. A false hub costs a route the reader might have
#: wanted; a missed hub costs a route the reader will believe.
DEFAULT_HUB_DEGREE = 25


@dataclass(frozen=True, slots=True)
class Hop:
    """One transfer along a route."""

    sender: str
    recipient: str
    at: Any
    """When. Required --- a hop with no timestamp cannot be ordered, and an
    unordered hop is exactly what makes a route unverifiable."""

    amount: int = 0
    symbol: str = ""
    asset: str = ""
    tx: str = ""

    def __str__(self) -> str:
        when = self.at.isoformat() if isinstance(self.at, datetime) else self.at
        return f"{self.sender} -> {self.recipient}  {self.amount} {self.symbol}  {when}"


@dataclass
class Route:
    """One time-respecting way the money could have travelled."""

    hops: list[Hop] = field(default_factory=list)
    forged_hops: int = 0
    """Hops moving an asset that failed the impersonation check.

    A token contract emits its own transfer events, so such a hop is not a
    movement of money --- it is a claim by whoever wrote the contract, which in
    this context is the person the route is about. A route built out of them is
    a route the attacker drew. Counted rather than dropped, because the reader
    may meet the same transfer elsewhere and needs to know what it is.
    """

    crosses_hub: str = ""
    """The hub this route passes through, if any. Empty when it crosses none.

    Populated rather than used to filter. A route through an exchange is a fact
    about the ledger; what it is not is evidence that these two addresses are
    connected, because the custodian commingled the funds. Saying which it is
    lets the reader decide, and hiding it would not.
    """

    @property
    def addresses(self) -> list[str]:
        return [self.hops[0].sender, *(h.recipient for h in self.hops)] if self.hops else []

    @property
    def length(self) -> int:
        return len(self.hops)

    @property
    def carries(self) -> int:
        """The most this route could have moved --- its narrowest hop.

        A route is a chain and this is its weakest link. Reported so that "a
        path exists" and "this path could have carried the sum in question"
        remain two claims rather than one.
        """
        return min((h.amount for h in self.hops), default=0)

    @property
    def elapsed(self) -> Any:
        if len(self.hops) < 2:
            return None
        try:
            return self.hops[-1].at - self.hops[0].at
        except TypeError:  # pragma: no cover - non-subtractable timestamps
            return None

    @property
    def is_believable(self) -> bool:
        """Whether every hop moved an asset that reports honestly."""
        return self.forged_hops == 0

    @property
    def single_asset(self) -> bool:
        """Whether every hop moved the same asset.

        A route that changes asset partway crossed something that swapped it,
        and the identity of the value on either side of that is an inference,
        not an observation.
        """
        assets = {h.asset for h in self.hops}
        return len(assets) == 1

    def describe(self) -> str:
        parts = [f"{self.length} hop(s)"]
        if self.hops:
            parts.append(f"carrying at most {self.carries} {self.hops[0].symbol or ''}".strip())
        if self.forged_hops:
            parts.append(
                f"but {self.forged_hops} of its {self.length} hop(s) move a token "
                f"that failed the impersonation check --- those transfers are "
                f"claims by whoever wrote that contract, not movements of money, "
                f"so this is a route the attacker drew"
            )
        if self.crosses_hub:
            parts.append(
                f"but it passes through {self.crosses_hub}, which has too many "
                f"counterparties to be anything but a service --- funds entering "
                f"it are commingled, so this is a link in the ledger and not a "
                f"link in the money"
            )
        if not self.single_asset:
            parts.append("the asset changes partway, so the two ends are not the same coin")
        return "; ".join(parts)


def hubs_in(transfers: list[Any], degree: int = DEFAULT_HUB_DEGREE) -> set[str]:
    """Addresses with more distinct counterparties than ``degree``.

    Degree over the data in hand, not over the chain. That is a real limitation
    and it errs the safe way: a store holding one case sees an exchange's three
    addresses rather than its three million, so a hub can be missed. It cannot
    be *invented*, though --- an address with 25 counterparties here has at
    least 25 --- so every hub reported is genuinely one.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for transfer in transfers:
        sender, recipient = _key(transfer, "sender"), _key(transfer, "recipient")
        if not sender or not recipient:
            continue
        seen[sender].add(recipient)
        seen[recipient].add(sender)
    return {address for address, others in seen.items() if len(others) > degree}


def _key(transfer: Any, field_name: str) -> str:
    value = getattr(transfer, field_name, None)
    if value is None:
        return ""
    raw = getattr(value, "key", None) or getattr(value, "raw", None) or value
    # `_fold`, not `.lower()`. The two must agree: the search normalises the
    # source and target with `_fold` and compares them against endpoints
    # normalised here, so lowercasing on this side alone meant a Solana, Sui or
    # Bitcoin address never matched itself --- every route missed, every
    # contributor reported unlinked, the subject not recognised as itself.
    #
    # Half-fixing a pair is worse than leaving both wrong: `.lower()` on both
    # sides at least agreed with itself.
    return _fold(str(raw))


def _instant(value: Any) -> float | None:
    """One comparable number for a timestamp, whatever shape it arrived in.

    A store returns `datetime`; a provider parsed straight from JSON returns
    Unix seconds. Sorting a list holding both raises `TypeError: '<' not
    supported between instances of 'int' and 'datetime.datetime'` --- from
    inside a sort, several frames from anything the caller recognises. Since
    the route search *is* comparison of times, one representation is used
    throughout and anything that cannot become one is not a hop.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # A naive datetime is read as local time by `.timestamp()`, which
        # shifts it by the machine's offset. Assumed UTC and said so, rather
        # than silently taking on the timezone of whoever is running this.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return float(value.timestamp())
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN compares False against everything, so `hop.at < since` was always
        # False and a NaN timestamp passed every ordering check --- manufacturing
        # a route rather than being rejected by one. Infinity is not a moment
        # either. Both are dropped and counted, like any undated transfer.
        return number if math.isfinite(number) else None
    return None


def _hop(transfer: Any) -> Hop | None:
    at = _instant(getattr(transfer, "timestamp", None))
    sender, recipient = _key(transfer, "sender"), _key(transfer, "recipient")
    if at is None or not sender or not recipient:
        # An undated transfer is dropped rather than assumed. Placing it
        # anywhere in the ordering would manufacture a route, and manufacturing
        # one is the failure this module is built against. The count of what
        # was dropped goes back to the caller.
        return None
    amount = getattr(transfer, "amount", None)
    asset = _key(transfer, "asset")
    return Hop(
        sender=sender,
        recipient=recipient,
        at=at,
        amount=int(getattr(amount, "raw", 0) or 0),
        symbol=str(getattr(amount, "symbol", "") or ""),
        asset=asset,
        tx=str(getattr(getattr(transfer, "tx", None), "hash", "") or ""),
    )


def _fold(address: str) -> str:
    """Normalise an address without knowing its chain.

    `fold_if_hex` folds only what is unambiguously a 42-character `0x` hex
    string and returns everything else exactly as given. `.lower()` here would
    be right on EVM and would destroy a base58 or bech32 address --- silently,
    by making two different addresses compare equal.
    """
    return fold_if_hex(address.strip())


def find_routes(
    transfers: list[Any],
    source: str,
    target: str,
    *,
    max_hops: int = 5,
    max_routes: int = 20,
    hub_degree: int = DEFAULT_HUB_DEGREE,
    allow_hubs: bool = False,
    chain: Any = None,
    max_steps: int = 200_000,
) -> tuple[list[Route], dict[str, Any]]:
    """Every time-respecting route from ``source`` to ``target``, bounded.

    Returns ``(routes, notes)``. ``notes`` carries what was excluded and why,
    because a list of routes with nothing said about what did not make the list
    reads as "this is all there is".

    The search is a depth-bounded enumeration over hops sorted by time, keeping
    only walks whose timestamps do not decrease. Each address appears at most
    once per route: a cycle adds hops without adding evidence, and letting the
    walk revisit turns a bounded search into an unbounded one.

    ``max_hops`` is the real cost control. The number of time-respecting walks
    grows quickly, and past five hops in a commingled graph the answer is
    "everything reaches everything", which is true and useless.
    """
    src, dst = _fold(source), _fold(target)
    hops: list[Hop] = []
    undated = duplicates = 0
    seen_hops: set[tuple[str, str, str, Any, int, str]] = set()
    for transfer in transfers:
        hop = _hop(transfer)
        if hop is None:
            undated += 1
            continue
        # One hop per transfer, however many times it was read. The analyzer
        # expands outward from both ends, so every transfer between two
        # expanded addresses arrives twice --- once from each side's history ---
        # and each copy multiplied the routes through it. A ledger read twice
        # produced four routes for one path.
        identity = (
            hop.tx,
            hop.sender,
            hop.recipient,
            hop.at,
            hop.amount,
            hop.asset,
        )
        if identity in seen_hops:
            duplicates += 1
            continue
        seen_hops.add(identity)
        hops.append(hop)

    hubs = hubs_in(transfers, hub_degree)
    trusted = trusted_assets(transfers, chain)
    outgoing: dict[str, list[Hop]] = defaultdict(list)
    for hop in sorted(hops, key=lambda h: h.at):
        outgoing[hop.sender].append(hop)

    routes: list[Route] = []
    hub_blocked = 0
    steps = 0

    def walk(node: str, since: Any, visited: set[str], trail: list[Hop]) -> None:
        nonlocal hub_blocked, steps
        # `max_routes` bounds what is *returned*; nothing bounded what is
        # *explored*. A dense graph where few walks reach the target searches
        # exponentially in `max_hops` and returns nothing, having spent the time
        # anyway --- and an empty result is read as "no route", not as "the
        # search gave up". Hitting this appears in the notes.
        if steps >= max_steps:
            return
        if len(routes) >= max_routes or len(trail) >= max_hops:
            return
        for hop in outgoing.get(node, ()):
            steps += 1
            if steps >= max_steps:
                return
            # The whole correction: a hop that happened before the money arrived
            # is not a way the money left. Measured on a real ledger, ignoring
            # this made 62% of returned multi-hop paths impossible.
            if since is not None and hop.at < since:
                continue
            if hop.recipient in visited:
                continue
            if hop.recipient == dst:
                walked = [*trail, hop]
                # From the ordered walk. Taking it from `visited` --- a set ---
                # meant that a route crossing two hubs named an arbitrary one of
                # them, chosen by hash order, so the same query could report a
                # different address between runs and neither was "the first hub
                # the money reached".
                crossed = next(
                    (h.sender for h in walked if h.sender in hubs and h.sender != src), ""
                )
                routes.append(
                    Route(
                        hops=walked,
                        crosses_hub=crossed,
                        forged_hops=sum(1 for h in walked if h.asset not in trusted),
                    )
                )
                if len(routes) >= max_routes:
                    return
                continue
            if hop.recipient in hubs and not allow_hubs:
                # Not silence: counted, and reported in `notes`. A route through
                # a custodian is a link in the ledger, not in the money.
                hub_blocked += 1
                continue
            walk(hop.recipient, hop.at, visited | {hop.recipient}, [*trail, hop])

    walk(src, None, {src}, [])

    # Shortest first, then by how much each could have carried. An investigator
    # reads the top of this list, so what is at the top matters.
    # Believable routes first, then shortest. A route made of the attacker's own
    # log entries should never be the first thing an investigator reads.
    routes.sort(key=lambda r: (not r.is_believable, r.length, -r.carries))

    notes: dict[str, Any] = {
        "hops_considered": len(hops),
        "hubs_detected": sorted(hubs),
        "routes_stopped_at_a_hub": hub_blocked,
        "max_hops": max_hops,
        "truncated": len(routes) >= max_routes,
        "steps": steps,
    }
    if steps >= max_steps:
        notes["search_budget_exhausted"] = (
            f"the walk stopped after {max_steps} steps. Routes beyond that point "
            f"were never explored, so an empty or short list here means 'as far "
            f"as the search got', not 'all there is'"
        )
    if undated:
        notes["undated_transfers_ignored"] = undated
    if duplicates:
        notes["duplicate_transfers_collapsed"] = duplicates
    return routes, notes


def findings(
    routes: list[Route], notes: dict[str, Any], source: str, target: str
) -> list[Finding]:
    """Turn routes into findings, including the finding that there are none."""
    src, dst = _fold(source), _fold(target)

    if not routes:
        # An empty result is a result, and it is the one most easily misread.
        detail = (
            f"No time-respecting route from {src} to {dst} within "
            f"{notes.get('max_hops')} hops, over {notes.get('hops_considered', 0)} "
            f"dated transfers in the store.\n\n"
            f"This is not proof the two are unconnected. It means no chain of "
            f"transfers *in this store* runs one to the other in forward time. "
            f"Funds that crossed a chain, an exchange, or an address the store "
            f"has never seen leave no such chain behind."
        )
        if notes.get("routes_stopped_at_a_hub"):
            detail += (
                f"\n\n{notes['routes_stopped_at_a_hub']} partial route(s) reached a "
                f"high-degree address and stopped there. Those are links in the "
                f"ledger, not in the money --- a custodian commingles what it "
                f"receives --- but if you want them anyway, re-run allowing hubs."
            )
        if notes.get("undated_transfers_ignored"):
            detail += (
                f"\n\n{notes['undated_transfers_ignored']} transfer(s) carry no "
                f"timestamp and were left out. A hop that cannot be ordered "
                f"cannot be shown to come after the one before it."
            )
        return [
            Finding(
                title=f"no route found from {src[:10]}… to {dst[:10]}…",
                severity=Severity.INFO,
                detail=detail,
                data={"routes": 0, **notes},
            )
        ]

    clean = [r for r in routes if not r.crosses_hub]
    out = [
        Finding(
            title=(
                f"{len(routes)} time-respecting route(s) from {src[:10]}… to "
                f"{dst[:10]}…, shortest {routes[0].length} hop(s)"
            ),
            severity=Severity.IMPORTANT if clean else Severity.NOTABLE,
            detail=(
                f"Each route's timestamps are non-decreasing, so each is a way the "
                f"money could have travelled in the order it actually happened. "
                f"That removes the impossible; it does not prove any one of them "
                f"is what occurred.\n"
                f"\n"
                f"  - {len(clean)} cross no high-degree address\n"
                f"  - {len(routes) - len(clean)} pass through one, and are links in "
                f"the ledger rather than in the money\n"
                f"  - the shortest carries at most {routes[0].carries} "
                f"{routes[0].hops[0].symbol or 'units'}, which is the ceiling on "
                f"what this route can account for"
            ),
            data={
                "routes": len(routes),
                "shortest_hops": routes[0].length,
                "without_hub": len(clean),
                **notes,
            },
        )
    ]

    for index, route in enumerate(routes[:5], start=1):
        out.append(
            Finding(
                title=f"route {index}: {' -> '.join(a[:10] + '…' for a in route.addresses)}",
                severity=Severity.NOTABLE if route.crosses_hub else Severity.IMPORTANT,
                detail=route.describe()
                + "\n\n"
                + "\n".join(f"  - {hop}" for hop in route.hops),
                data={
                    "addresses": route.addresses,
                    "hops": route.length,
                    "carries": route.carries,
                    "crosses_hub": route.crosses_hub or None,
                    "single_asset": route.single_asset,
                    # A consumer reading `data` never saw this, so a route made
                    # entirely of the attacker's own log entries was
                    # indistinguishable in JSON from one built out of real
                    # transfers. It is in the title and the detail; it belongs
                    # where a machine reads.
                    "forged_hops": route.forged_hops,
                    "believable": route.is_believable,
                    "transactions": [h.tx for h in route.hops if h.tx],
                },
            )
        )
    return out


def _fetch(ctx: Context, address: str, limit: int) -> Any:
    """A reader for one address's transfers, bound now rather than at call time.

    A closure over the loop variable would have every fetch read whichever
    address the loop had reached by the time the router got round to calling it.
    """

    def read(provider: Any) -> Any:
        return provider.asset_transfers(ctx.chain, address, direction="all", limit=limit)

    return read


class RouteAnalyzer(Analyzer):
    """How money could have got from one address to another."""

    name = "route"
    description = "find time-respecting routes between two addresses"
    requires = Capability.ASSET_TRANSFERS

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, self.requires))

    def run(
        self,
        ctx: Context,
        *,
        source: str = "",
        target: str = "",
        max_hops: int = 5,
        allow_hubs: bool = False,
        max_expand: int = 60,
        **_: Any,
    ) -> Result:
        if not source or not target:
            raise ValueError("route analysis needs both a `source` and a `target` address")
        started = datetime.now(timezone.utc)
        src = address_key(ctx.chain, source)
        dst = address_key(ctx.chain, target)
        per_node = ctx.limit("per_node", 1000)

        # Both ends, and every hop the search may cross. Reading only the
        # source's history finds nothing past the first hop --- the second hop
        # is somebody else's history --- so the frontier is expanded outward.
        #
        # Hard-bounded, and the bound is the whole design. Left to grow, the
        # frontier is the transitive closure of the chain: the first run of this
        # against a real address was still fetching after two minutes, because
        # one counterparty had thousands of its own. `max_expand` caps how many
        # addresses are ever read, and busiest-last ordering spends that budget
        # on the addresses most likely to be a genuine step rather than on a
        # service. A route that needed an unexplored address is reported as not
        # found *within what was read*, never as not found.
        seen: dict[str, Any] = {}
        frontier = [src, dst]
        notes: list[str] = []
        too_busy: list[str] = []
        exhausted = False
        _degree: dict[str, int] = {}
        # Named, not `_`: the signature's `**_` catch-all is already bound to a
        # dict in this scope, so reusing the name reassigns it to an int.
        for _depth in range(min(max_hops, ctx.limit("max_depth", 3))):
            nxt: list[str] = []
            for address in frontier:
                if address in seen:
                    continue
                if len(seen) >= max_expand:
                    # An explicit flag rather than emptying `frontier`. Clearing
                    # it worked only because the outer loop happened to read the
                    # variable afterwards; reordering those two statements would
                    # have silently resumed expanding, and nothing would have
                    # failed.
                    notes.append(
                        f"stopped after reading {len(seen)} addresses (max_expand="
                        f"{max_expand}). Any route whose middle lies outside that "
                        f"set was not searched, so 'no route' means 'none within "
                        f"what was read'"
                    )
                    exhausted = True
                    break
                try:
                    rows, source_notes = history_of(ctx, _fetch(ctx, address, per_node))
                except ProviderError as exc:
                    # An address whose history will not fit in one page *is* a
                    # hub, and that is the useful reading of this failure rather
                    # than a reason to abandon the search. The provider refuses
                    # to return a silently short answer, which is correct; here
                    # it means "too busy to be a step on a route", so it is
                    # recorded as a dead end and the search continues.
                    seen[address] = []
                    too_busy.append(address)
                    notes.append(
                        f"{address} has more history than one page holds, so it was "
                        f"not expanded through. An address that busy commingles "
                        f"anyway: a route across it would be a link in the ledger, "
                        f"not in the money ({exc})"
                    )
                    continue
                notes.extend(source_notes)
                seen[address] = rows
                for row in rows:
                    for end in ("sender", "recipient"):
                        other = _key(row, end)
                        if not other:
                            continue
                        _degree[other] = _degree.get(other, 0) + 1
                        if other not in seen:
                            nxt.append(other)
            if exhausted:
                break
            if not frontier and _depth:
                break
            # Least-connected first. The budget should be spent on addresses
            # that might be a step on a route, and an address appearing in
            # dozens of transfers is a service, whose expansion produces
            # thousands of neighbours and no usable hop.
            frontier = sorted(dict.fromkeys(nxt), key=lambda a: _degree.get(a, 0))
            if len(seen) >= ctx.limit("max_nodes", 200):
                notes.append(
                    f"stopped expanding at {len(seen)} addresses (max_nodes). A "
                    f"route whose middle lies beyond that was not searched, so "
                    f"'no route' below means 'none within what was read'"
                )
                break

        transfers = [row for rows in seen.values() for row in rows]
        routes, found_notes = find_routes(
            transfers, src, dst, max_hops=max_hops, allow_hubs=allow_hubs, chain=ctx.chain
        )
        if found_notes.get("routes_stopped_at_a_hub") and not allow_hubs:
            notes.append(
                f"{found_notes['routes_stopped_at_a_hub']} partial route(s) reached "
                f"a high-degree address and stopped. Pass allow_hubs=true to see "
                f"them, remembering that a custodian commingles what it receives"
            )
        if found_notes.get("truncated"):
            notes.append("more routes exist than were returned; raise max_routes to see them")
        if too_busy and not routes:
            notes.append(
                f"{len(too_busy)} address(es) on the way were too busy to expand "
                f"through. If the two ends are connected only via one of those, "
                f"no route can be found here --- and a route across it would not "
                f"have been evidence anyway"
            )

        return Result(
            analyzer=self.name,
            findings=tuple(findings(routes, found_notes, src, dst)),
            warnings=tuple(dict.fromkeys(notes)),
            evidence=ctx.evidence(),
            # Every input that changed the answer, not just the two addresses.
            # `allow_hubs` decides whether a route is offered at all and
            # `max_expand` decides how much was searched, so a params block
            # without them describes a run nobody can repeat --- and "no route"
            # is the result most in need of repeating.
            params={
                "source": source,
                "target": target,
                "chain": str(ctx.chain),
                "max_hops": max_hops,
                "allow_hubs": allow_hubs,
                "max_expand": max_expand,
                "per_node": per_node,
                # The resolved depth, not the requested one. `max_depth` is a
                # context limit and `max_hops` an argument, and the search uses
                # the smaller --- so recording either alone describes a run that
                # may not reproduce.
                "max_depth": min(max_hops, ctx.limit("max_depth", 3)),
                "addresses_read": len(seen),
            },
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
