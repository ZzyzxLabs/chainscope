"""Build a :class:`Screen` from a store: walk the money backwards.

Everything else in this package is a type. Nothing in it had ever read a
transfer, which meant the exposure model, the policy engine and the decision
record could all be exercised only against hand-built fixtures --- and a
screening system that has never been pointed at a chain is a schema, not a
control. This is the join.

**The walk goes backwards, and that is the whole point.** "Where did this money
go" is the question people ask of a graph; "who paid this address" is the
question that decides whether you can accept a deposit. In the LpdFi case they
had different answers: the attacker's outbound path led to a router that pooled
a thousand strangers, and the *inbound* path led to a funder who staked them two
thousand blocks before the exploit. A screen that only looked forward saw
nothing.

Four ways the walk can end, and they are not the same fact:

``EXHAUSTED``
    Nothing paid this address. The trace reached an origin.

``SERVICE``
    The funder pools unrelated customers --- degree above
    :data:`~chainscope.analysis.trail.SERVICE_DEGREE`, or an attributed
    terminal category. Value arriving from a hot wallet says nothing about
    any individual, so continuing would attribute a thousand strangers'
    money to one of them. The path is a **prefix**.

``HOP_LIMIT``
    Ran out of configured depth. Says nothing about what lies beyond.

``UNREACHABLE``
    The address was never fetched, or a source failed. The path is short
    because the *lookup* was short, not because the money was.

Only the first makes :attr:`Screen.complete` true, and `Policy.choose` already
refuses to release funds on an incomplete screen. So a chain we could not read
can never emerge at the far end as "clean" --- which is the failure mode the
whole package was written against.

**On the taint model.** Shares at hop 0 are arithmetic: this funder's inbound
total over all inbound. Beyond hop 0 they are proportional --- a funder's share
is its parent's share times its fraction of the parent's inbound --- which is
the *haircut* rule, and it is named as such in `Screen.taint` rather than
labelled FIFO for familiarity. FIFO's advantage is lot-level provenance, and
recovering it requires every intermediary's full transfer history in block
order; a store holds that only where the address was expanded, so claiming FIFO
across a partly-expanded case would be claiming a precision the data does not
carry. Where the full history *is* in hand, `risk.agreement.compare_taint_models`
runs all three and reports the disagreement, which is the honest form of the
question.

**One asset at a time.** Shares are fractions of a deposit, and a fraction
across two assets has no meaning without a price --- which this package does not
have and will not invent. The walk therefore follows one asset and says so; a
swap along the path ends that branch, recorded as a note, because pretending to
follow value through a conversion we cannot price is worse than stopping.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..analysis.trail import _DUST_SHARE, SERVICE_DEGREE
from ..attribution.resolver import Resolution, Resolver
from ..chains import address_key
from ..core.attribution import Attribution, Confidence, Method
from ..core.chainid import ChainId
from ..core.entity import Entity, Membership
from ..core.units import Amount
from ..store.base import EdgeSummary, Query, Store
from .exposure import Exposure, Screen, Signal, StopReason

__all__ = ["Reached", "screen", "shape_signals"]


#: How far back the walk goes before giving up.
#:
#: Four rather than "as far as it goes". Each hop back multiplies the addresses
#: under examination, and an unbounded walk on a busy address returns the
#: chain's history --- but the number is not the safety property. Stopping is
#: recorded as ``HOP_LIMIT``, which makes the screen incomplete, which makes
#: `allow` unreachable. A depth that is too small produces a hold, not a false
#: clearance.
DEFAULT_HOPS = 4


@dataclass(frozen=True, slots=True)
class Reached:
    """One address the backward walk arrived at, and how.

    Kept separate from :class:`Exposure` because most of what a walk reaches is
    unattributed, and an exposure requires evidence. This is the raw shape of
    the walk; the exposures are the subset somebody has said something about.
    """

    address: str
    hops: int
    path: tuple[str, ...]
    """Between the deposit and this address, nearest first. Empty at hop 0."""

    share: Decimal
    amount_raw: int
    stopped: StopReason
    degree: int
    """Distinct counterparties seen in the store. The service test, and it
    under-reports on a partly-fetched case --- see `_looks_like_a_service`."""


def _looks_like_a_service(store: Store, address: str, chain: ChainId, degree: int) -> bool:
    """Whether this address pays and is paid by more parties than a person.

    Counted over what the store holds, so it under-reports: an address the case
    has barely seen looks quiet. That direction is the safe one here for the
    same reason it is in `analysis.trail` --- a missed boundary shows up as a
    walk that continued, which a reader can see and argue with, where a false
    boundary shows up as a walk that stopped, which looks identical to the
    money stopping.
    """
    peers = {e.sender for e in store.edges(address, chain, direction="in")}
    peers |= {e.recipient for e in store.edges(address, chain, direction="out")}
    peers.discard(address_key(chain, address))
    return len(peers) >= degree


def _inbound(
    store: Store, address: str, chain: ChainId, asset: str | None
) -> tuple[list[EdgeSummary], list[EdgeSummary]]:
    """Inbound edges in one asset: ``(worth walking, folded)``.

    The fold is the address-poisoning defence, and without it this walk is
    unusable on exactly the addresses it is for. The LpdFi attacker was paid by
    three real parties and by seventy-nine addresses whose hex was chosen to
    resemble the real funder's --- `0xa16f0de5…8968` and `0xa16ffe91…8968`
    against a genuine `0xa16f5ba4…8968`. Every one of them is an inbound edge
    with a positive amount, so every one of them was walked, resolved, and
    reported as a funding path whose origin is unknown. The screen's list of
    gaps was seventy-nine lines of somebody else's spam.

    Folded at :data:`~chainscope.analysis.trail._DUST_SHARE` of the largest
    inbound edge in the same asset --- the threshold `analysis.trail` measured
    rather than picked, chosen with two orders of magnitude of room on either
    side of the gap between a deliberate 100 USDC test payment and 0.0000689
    USDC of dust.

    **Folded, not dropped.** The second list comes back so the caller can say
    how many and how much, because a funding picture that silently became
    legible is one somebody trusts for the wrong reason. And the fold is safe
    for the only thing this walk feeds: a share below a millionth cannot reach
    a policy threshold, so nothing folded here could have changed an answer.
    """
    edges = [
        e
        for e in store.edges(address, chain, direction="in")
        if (e.asset or None) == asset and e.total_raw > 0
    ]
    if not edges:
        return [], []
    floor = int(Decimal(max(e.total_raw for e in edges)) * _DUST_SHARE)
    return (
        [e for e in edges if e.total_raw >= floor],
        [e for e in edges if e.total_raw < floor],
    )


def _display(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals) if decimals > 0 else Decimal(raw)


def _pick_asset(store: Store, address: str, chain: ChainId) -> EdgeSummary | None:
    """The asset the largest quantity arrived in.

    **Quantity, not value, and the difference is the whole caveat.** Ranking
    assets by value needs a price at the time of the deposit, which this package
    does not have and will not invent, so this is a starting point rather than a
    judgement --- and the note it produces says so and names the alternatives. A
    caller who knows which deposit they are screening should pass ``asset``.

    Ranked in display units rather than raw. Raw integers of different assets
    are different things: 1 token at eighteen decimals is 1e18 raw and 689,529
    USDC at six is 6.9e11, so a raw comparison hands the screen to whichever
    asset has the most decimals, every time. `analysis.trail._minor_below`
    documents this exact trap for thresholds and I walked into it here for
    ranking.
    """
    totals: dict[str | None, EdgeSummary] = {}
    for edge in store.edges(address, chain, direction="in"):
        if edge.total_raw <= 0:
            continue
        key = edge.asset or None
        if key in totals:
            prior = totals[key]
            totals[key] = EdgeSummary(
                sender="",
                recipient=prior.recipient,
                asset=prior.asset,
                total_raw=prior.total_raw + edge.total_raw,
                transfer_count=prior.transfer_count + edge.transfer_count,
                symbol=prior.symbol,
                decimals=prior.decimals,
            )
        else:
            totals[key] = edge
    if not totals:
        return None
    return max(totals.values(), key=lambda e: _display(e.total_raw, e.decimals))


def _named_asset(store: Store, address: str, chain: ChainId, asset: str) -> EdgeSummary | None:
    """Inbound totals for the asset the caller named, or None if none arrived.

    Matched case-insensitively against the contract address, and against the
    literal ``native`` for a chain's own coin --- which has no contract, so
    ``None`` is its identity in every row and there is otherwise no way to ask
    for it.
    """
    wanted = asset.strip().lower()
    native = wanted in ("native", "")
    edges = [
        e
        for e in store.edges(address, chain, direction="in")
        if e.total_raw > 0
        and ((e.asset is None) if native else (e.asset or "").lower() == wanted)
    ]
    if not edges:
        return None
    first = edges[0]
    return EdgeSummary(
        sender="",
        recipient=first.recipient,
        asset=first.asset,
        total_raw=sum(e.total_raw for e in edges),
        transfer_count=sum(e.transfer_count for e in edges),
        symbol=first.symbol,
        decimals=first.decimals,
    )


def _walk(
    store: Store,
    address: str,
    chain: ChainId,
    asset: str | None,
    *,
    hops: int,
    service_degree: int,
    resolved: dict[str, Resolution],
    resolver: Resolver | None,
) -> tuple[list[Reached], int, int]:
    """Breadth-first backwards.

    Returns what was reached, how many inbound edges were folded as dust, and
    their combined raw amount --- the last two so the caller can state the fold
    rather than let the picture quietly improve.

    Breadth-first rather than depth-first so that the *shortest* path to any
    address is the one recorded. An address reachable at one hop and again at
    four is one hop away; recording the four would overstate the distance
    between a deposit and a sanctioned entity, which is precisely the number a
    policy is written against.
    """
    root = address_key(chain, address)
    seen: set[str] = {root}
    out: list[Reached] = []
    folded = folded_raw = 0

    queue: deque[tuple[str, int, tuple[str, ...], Decimal]] = deque(
        [(address, 0, (), Decimal(1))]
    )
    while queue:
        here, depth, path, share = queue.popleft()

        edges, dust = _inbound(store, here, chain, asset)
        folded += len(dust)
        folded_raw += sum(e.total_raw for e in dust)
        total = sum(e.total_raw for e in edges)
        if not edges or total <= 0:
            continue

        for edge in edges:
            funder = edge.sender
            key = address_key(chain, funder)
            if key == root or key in seen:
                continue
            seen.add(key)

            portion = share * (Decimal(edge.total_raw) / Decimal(total))
            step = Reached(
                address=funder,
                hops=depth,
                path=path,
                share=min(Decimal(1), portion),
                amount_raw=edge.total_raw,
                stopped=StopReason.EXHAUSTED,
                degree=0,
            )

            if resolver is not None and key not in resolved:
                resolved[key] = resolver.resolve(funder, chain)
            claim = resolved.get(key)

            # Why this branch ends, checked in the order that makes the
            # strongest statement win. A service boundary is a deliberate stop
            # and stays a service boundary even at the hop limit; an unfetched
            # address is unknown regardless of how deep it sits.
            service = (claim is not None and claim.category.is_terminal) or (
                _looks_like_a_service(store, funder, chain, service_degree)
            )
            if service:
                step = _ended(step, StopReason.SERVICE)
            elif not store.is_expanded(funder, chain):
                # Never *followed*. Distinct from never seen, and the wording of
                # the gap depends on which --- see `_gap`.
                step = _ended(step, StopReason.UNREACHABLE)
            elif depth + 1 >= hops:
                step = _ended(step, StopReason.HOP_LIMIT)
            else:
                queue.append((funder, depth + 1, (*path, funder), portion))

            out.append(step)

    return out, folded, folded_raw


def _ended(step: Reached, why: StopReason) -> Reached:
    return Reached(
        address=step.address,
        hops=step.hops,
        path=step.path,
        share=step.share,
        amount_raw=step.amount_raw,
        stopped=why,
        degree=step.degree,
    )


def _entity_for(reached: Reached, claim: Resolution, chain: ChainId) -> Entity:
    """The party an exposure is *to*, built from what the sources said.

    One address, not a cluster. Clustering the funder into an entity would
    widen the exposure to every address believed to be the same party, and
    doing that silently --- on the strength of a co-spend heuristic nobody in
    this call chain has seen --- is how a guess becomes the basis of a freeze.
    `core.entity` exists for the case where the membership evidence is in hand;
    this is not that case.
    """
    entity = claim.entity
    return Entity(
        key=address_key(chain, reached.address),
        name=claim.label,
        category=claim.category,
        members=(
            Membership(
                address=reached.address,
                chain=chain,
                confidence=claim.confidence,
                source=entity.primary.source if entity else "store",
                rationale="the address itself; no clustering was applied",
            ),
        ),
    )


def _claims(claim: Resolution, address: str, chain: ChainId) -> tuple[Attribution, ...]:
    """What the exposure rests on. Never empty --- `Exposure` refuses that."""
    if claim.entity is not None:
        return tuple(claim.entity.all_claims)
    return (
        Attribution(
            address=address,
            chain=chain,
            label=claim.label,
            category=claim.category,
            confidence=Confidence.SPECULATIVE,
            method=Method.HEURISTIC,
            source="chainscope-screen",
            rationale="reached by walking inbound transfers; nobody has labelled it",
        ),
    )


#: How many transfers an address can have and still be called new.
#:
#: Not a measurement --- it is a description of the shape "funded once and
#: forwarding", and the shape stops being that once there is a history to read.
#: Twenty is generous on purpose: the signal is capped at MEDIUM and cannot
#: reach an irreversible action, so its cost when wrong is a review.
_QUIET = 20


def shape_signals(store: Store, address: str, chain: ChainId) -> tuple[Signal, ...]:
    """What the shape of the money says, where nobody has attributed anything.

    The half that works on the day of an incident. At the moment an exploit
    happens nobody has labelled the attacker, so a screen built only on
    attribution answers "clean" to the most dangerous deposit it will see all
    year --- correctly, and uselessly. These are observations about structure
    instead, and every one of them is consistent with an innocent explanation,
    which is why `Signal` caps itself at MEDIUM and the policy layer will not
    let one justify returning somebody's money.

    Computed from the store alone. A signal that needed the network would not
    be available at the moment it is worth having.
    """
    rows = list(store.transfers(Query(chain=chain, address=address, limit=5000)))
    if not rows:
        return ()

    out: list[Signal] = []
    key = address_key(chain, address)
    inbound = [r for r in rows if r.recipient and address_key(chain, r.recipient.raw) == key]
    outbound = [r for r in rows if r.sender and address_key(chain, r.sender.raw) == key]

    if inbound and outbound and len(rows) <= _QUIET:
        blocks = [r.block for r in rows if r.block is not None]
        span = max(blocks) - min(blocks) if blocks else 0
        out.append(
            Signal(
                name="fresh_address",
                summary=(
                    f"{len(rows)} transfers in total, spanning {span} block(s): "
                    f"funded and forwarding, with no history behind it"
                ),
                detail=(
                    "Also what a person moving to a new wallet looks like. It is a "
                    "reason to look, not a reason to conclude."
                ),
                confidence=Confidence.LOW,
            )
        )

    if len(inbound) >= 5 and len(outbound) <= 2:
        out.append(
            Signal(
                name="consolidation",
                summary=(
                    f"{len(inbound)} inbound against {len(outbound)} outbound: "
                    f"value gathered here from many places and left by few"
                ),
                detail="The shape of a collection point. Also the shape of a savings account.",
                confidence=Confidence.LOW,
            )
        )

    try:
        from ..analysis.probing import detect_probes

        probes = [
            p
            for p in detect_probes(rows)
            if address_key(chain, p.source) == key or address_key(chain, p.destination) == key
        ]
    except Exception:  # pragma: no cover - a signal must not break a screen
        probes = []
    for probe in probes:
        out.append(
            Signal(
                name="probing",
                summary=(
                    f"{probe.kind} toward {probe.destination}: "
                    f"{probe.steps} amounts, reaching {probe.growth:.0f}x"
                ),
                detail=(
                    f"Amounts in order: {', '.join(str(a) for a in probe.amounts[:6])}. "
                    f"One in {1 / probe.chance:.0f} by chance if the order meant nothing."
                ),
                confidence=Confidence.MEDIUM,
            )
        )

    try:
        from ..analysis.trail import trail as build_trail

        found = build_trail(rows, address, chain=chain)
    except Exception:  # pragma: no cover
        found = None
    if found is not None and found.forged_assets:
        out.append(
            Signal(
                name="impersonation",
                summary=(
                    f"{len(found.forged_assets)} asset(s) touching this address "
                    f"imitate a canonical token"
                ),
                detail=(
                    "A forged token's logs say whatever its author chose, including "
                    "who sent them. Their presence says this address was targeted "
                    "by a poisoning campaign; it says nothing about the address."
                ),
                confidence=Confidence.MEDIUM,
            )
        )

    return tuple(out)


def _where(step: Reached) -> str:
    return "a direct funder" if step.hops == 0 else f"{step.hops} hop(s) back"


def _gap(step: Reached, store: Store, chain: ChainId) -> str:
    """One line naming a walk that ended somewhere other than an origin.

    The ``UNREACHABLE`` wording is split on whether the store holds any edges
    for the address, and the split is not pedantry. `is_expanded` records that
    an address was *followed*; it does not record that it was never fetched,
    and stores written before the mark existed have transfers for every address
    and no marks at all. Saying "never fetched" about those was a claim the
    store cannot support --- and it was made about `0xa16f5ba4…8968`, whose
    forty-five transfers were sitting in the same file.
    """
    where = _where(step)
    if step.stopped is StopReason.SERVICE:
        return (
            f"the walk stopped at {step.address} ({where}), which behaves like a "
            f"service --- what funded it is pooled with unrelated customers' "
            f"money, so this path is a prefix rather than an origin"
        )
    if step.stopped is StopReason.HOP_LIMIT:
        return (
            f"the walk stopped at {step.address} ({where}) on the hop limit; "
            f"nothing is claimed about what funded it"
        )
    if store.edges(step.address, chain, direction="in"):
        return (
            f"{step.address} ({where}) was never followed, so what is held for it "
            f"is whatever leaked in from its neighbours --- a fragment, not its "
            f"funding history"
        )
    return (
        f"{step.address} ({where}) has never been fetched, so its funders are "
        f"unknown. The path is short because the lookup was"
    )


def screen(
    store: Store,
    address: str,
    chain: ChainId,
    *,
    resolver: Resolver | None = None,
    asset: str | None = None,
    amount: Amount | None = None,
    at: datetime | None = None,
    hops: int = DEFAULT_HOPS,
    service_degree: int = SERVICE_DEGREE,
    signals: Sequence[Signal] = (),
) -> Screen:
    """Screen what arrived at ``address``, by walking backwards from it.

    ``asset`` is the token contract to follow. Omitted, the largest inbound
    quantity is used and the screen says which and what it passed over ---
    ranking assets without a price is not a value judgement and must not read
    like one. ``amount`` defaults to everything that arrived in it; pass one to
    ask about a specific deposit instead, and the shares become fractions of
    that.

    Nothing here decides anything. The result is the input to a `Policy`, and
    the separation is deliberate: this layer describes what was found and what
    was not, and the customer's rule set --- named, versioned, ordered --- says
    what to do about it.
    """
    notes: list[str] = []
    unreachable: list[str] = []

    dominant = _named_asset(store, address, chain, asset) if asset else None
    if asset and dominant is None:
        # Asked about an asset that never arrived. Not the same as a clean
        # screen in that asset, and not the same as a typo going unnoticed.
        unreachable.append(
            f"nothing arrived at {address} in asset {asset} in the data held, "
            f"so there was nothing to screen"
        )
        return Screen(
            address=address,
            amount=amount or Amount(0, 18),
            at=at or datetime.now(timezone.utc),
            taint="haircut",
            signals=tuple(signals),
            unreachable_sources=tuple(unreachable),
            notes=tuple(notes),
        )
    if dominant is None:
        dominant = _pick_asset(store, address, chain)
    if dominant is None:
        # No inbound at all. Two very different reasons, and the store knows
        # which: nothing arrived, or nobody looked.
        if not store.is_expanded(address, chain):
            unreachable.append(
                f"{address} has never been fetched on {chain}; this is a "
                f"statement about the store, not about the address"
            )
        else:
            notes.append("nothing arrived at this address in the data held.")
        return Screen(
            address=address,
            amount=amount or Amount(0, 18),
            at=at or datetime.now(timezone.utc),
            taint="haircut",
            signals=tuple(signals),
            unreachable_sources=tuple(unreachable),
            notes=tuple(notes),
        )

    chosen = dominant.asset or None
    deposit = amount or Amount(dominant.total_raw, dominant.decimals, dominant.symbol)

    others = {
        (e.symbol or e.asset or "native")
        for e in store.edges(address, chain, direction="in")
        if (e.asset or None) != chosen and e.total_raw > 0
    }
    if others:
        notes.append(
            f"screened in {dominant.symbol or 'the largest inbound asset'} only, "
            f"chosen by quantity because ranking assets by value would need a "
            f"price this does not have. Value also arrived in "
            f"{', '.join(sorted(others))} and none of it was walked; pass the "
            f"asset explicitly to screen one of those instead."
        )

    resolved: dict[str, Resolution] = {}
    reached, folded, folded_raw = _walk(
        store,
        address,
        chain,
        chosen,
        hops=hops,
        service_degree=service_degree,
        resolved=resolved,
        resolver=resolver,
    )
    if folded:
        notes.append(
            f"{folded} inbound edge(s) totalling "
            f"{_display(folded_raw, deposit.decimals):f} {deposit.symbol or 'units'} "
            f"were folded as dust and not walked. On a poisoned address these are "
            f"addresses whose hex was chosen to resemble a real funder's; their "
            f"combined share cannot reach any policy threshold, and they are "
            f"counted here rather than removed silently."
        )

    exposures: list[Exposure] = []
    for step in reached:
        claim = resolved.get(address_key(chain, step.address))
        if claim is None or not claim.found:
            # Reached, unattributed, and the walk stopped there anyway. This
            # branch is the one that made the first version of this function
            # wrong: an `Exposure` carries its `stopped_at`, so a service
            # boundary or a hop limit shows up in `Screen.complete` --- but
            # only where somebody had labelled the address. Where nobody had,
            # the stop vanished, and a walk that halted at a router reported
            # itself complete. Verified on the LpdFi attacker, which came back
            # complete with the trail sitting against an unlabelled service.
            if step.stopped is not StopReason.EXHAUSTED:
                unreachable.append(_gap(step, store, chain))
            continue
        if not claim.reliable:
            unreachable.extend(f"{name}: {err}" for name, err in claim.failed)
        exposures.append(
            Exposure(
                source=_entity_for(step, claim, chain),
                category=claim.category,
                amount=Amount(
                    int(step.share * Decimal(deposit.raw)), deposit.decimals, deposit.symbol
                ),
                share=step.share,
                hops=step.hops,
                path=step.path,
                evidence=_claims(claim, step.address, chain),
                stopped_at=step.stopped,
            )
        )

    if resolver is None or not resolver.sources:
        # An absent lookup is not a clean one. Without this the screen would
        # report zero exposures with `complete` true --- the exact shape this
        # package exists to refuse.
        #
        # `not resolver.sources` and not just `is None`: a `Resolver` built
        # against a label directory that does not exist is a perfectly valid
        # object that answers "unknown" to everything, and that answer is
        # indistinguishable from a real one at every call site downstream.
        unreachable.append(
            "no attribution source was consulted, so nothing could be "
            "attributed. This is a statement about the configuration, not "
            "about the addresses"
        )

    stops = {step.stopped for step in reached}
    if StopReason.SERVICE in stops:
        notes.append(
            "one or more funders behave like services. The walk stops at them: "
            "their funds are pooled with unrelated customers', so continuing "
            "would trace shared infrastructure rather than this money."
        )
    if StopReason.HOP_LIMIT in stops:
        notes.append(f"the walk stopped at {hops} hops. Nothing is claimed beyond that.")

    notes.append(
        f"{len(reached)} address(es) reached going back from the deposit; "
        f"{len(exposures)} carried an attribution."
    )

    return Screen(
        address=address,
        amount=deposit,
        at=at or datetime.now(timezone.utc),
        taint="haircut",
        exposures=tuple(exposures),
        signals=tuple(signals),
        unreachable_sources=tuple(dict.fromkeys(unreachable)),
        notes=tuple(notes),
    )
