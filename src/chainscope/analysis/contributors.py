"""Who paid into this address, and which of them has anything to do with the subject.

The step that separates a defensible total from an inflated one, and the one a
methodology written from real cases puts at the end of its noise-filtering list.

The situation: a deposit address is identified as the subject's, its inbound
total is computed, and the figure goes in the report. In one real case that
address had also received **3 ETH from a completely unrelated party** — an
address with 122 transactions of its own, funded from a different 500 ETH, and
itself a victim of the same poisoning campaign. Included, it inflates "the
subject sent N ETH to this service" by exactly 3 ETH, and nothing about the
number looks wrong.

A deposit address is a *destination*, not a private channel. Anybody may pay it.

**This module does not decide the total. It decomposes it.** Every contributor
is placed in one of four buckets, and the buckets are reported separately so a
reader chooses what to claim:

``self``
    The subject itself. The uncontroversial part of any total.

``reachable``
    A time-respecting route runs from the subject to this contributor. Its money
    plausibly *is* the subject's money, one or more hops along.

``co_funded``
    It shares a first funder with the subject. Weaker than a route and worth
    saying separately, because an exchange funds thousands of unrelated
    customers — :mod:`chainscope.analysis.funding` measures that failure and it
    is severe.

``unlinked``
    Nothing in this store connects it to the subject. **Not "unrelated".** The
    store holds one case; an address related through a hop nobody fetched sits
    here too, and so does a genuine stranger. The distinction cannot be made
    from absence, so the bucket is named for what is true of it.

**The refusals.**

It will not subtract anything. Producing "the corrected total" would hide the
judgement inside a number, and the judgement — whether an unlinked contributor
is a stranger or an unexplored hop — is the reader's, made with context this
code does not have.

It will not call ``unlinked`` clean. Every report of it states how far the
search actually went, because "no link found" over a store holding two hops is a
different statement from the same words over a store holding twenty.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..chains import address_key, fold_if_hex
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability, ProviderError
from .base import Analyzer, Context, history_of
from .route import find_routes

__all__ = [
    "Contribution",
    "ContributorsAnalyzer",
    "Inflow",
    "Link",
    "contributors",
    "findings",
]


class Link:
    """How a contributor connects to the subject, if it does.

    A `str` enum with no ordering. `reachable` and `co_funded` are different
    kinds of evidence rather than degrees of one, and ranking them would invite
    a comparison that means nothing.
    """

    SELF = "self"
    REACHABLE = "reachable"
    CO_FUNDED = "co_funded"
    UNLINKED = "unlinked"


@dataclass(frozen=True, slots=True)
class Contribution:
    """One address's inbound payments to the target, and its link to the subject."""

    address: str
    amount: int
    transfers: int
    symbol: str = ""
    decimals: int | None = None
    """Carried, never assumed. Rendering 750 ETH at the wrong scale is the
    defect that put `0.000000` on the dashboard where 1,000 USDC belonged."""

    link: str = Link.UNLINKED
    detail: str = ""
    """Why this link, in a form the reader can check --- the route's hops, or
    the funder both addresses share."""

    @property
    def is_attributable(self) -> bool:
        """Whether this contributor's payments may be counted as the subject's."""
        return self.link in (Link.SELF, Link.REACHABLE)


@dataclass
class Inflow:
    """Everything that reached one address, decomposed by who sent it."""

    target: str
    subject: str
    contributions: list[Contribution] = field(default_factory=list)
    hops_searched: int = 0
    transfers_searched: int = 0

    def _by(self, *links: str) -> list[Contribution]:
        return [c for c in self.contributions if c.link in links]

    @property
    def total(self) -> int:
        return sum(c.amount for c in self.contributions)

    @property
    def attributable(self) -> int:
        """What the subject can be said to have sent, directly or through hops."""
        return sum(c.amount for c in self.contributions if c.is_attributable)

    @property
    def unlinked(self) -> int:
        return sum(c.amount for c in self._by(Link.UNLINKED))

    @property
    def symbol(self) -> str:
        return next((c.symbol for c in self.contributions if c.symbol), "")

    @property
    def decimals(self) -> int | None:
        return next((c.decimals for c in self.contributions if c.decimals is not None), None)

    def show(self, raw: int) -> str:
        """An amount a person can read, or the raw integer said to be raw.

        Never scaled by a guess. A figure rendered at the wrong number of
        decimals is off by a factor of a million and looks entirely normal.
        """
        from ..render.amount import human

        rendered = human(str(raw), self.decimals)
        return (
            f"{rendered} {self.symbol or ''}".strip() if self.decimals is not None else rendered
        )

    def summary(self) -> str:
        if not self.contributions:
            return f"nothing reached {self.target} in this store"
        if not self._by(Link.UNLINKED, Link.CO_FUNDED):
            return (
                f"{self.show(self.total)} reached {self.target}, all of it from "
                f"the subject or from addresses the subject can be traced to"
            )
        return (
            f"{self.show(self.total)} reached {self.target}. "
            f"{self.show(self.attributable)} is the subject's or traceable to "
            f"it; {self.show(self.unlinked)} came from "
            f"{len(self._by(Link.UNLINKED))} address(es) with no link to the "
            f"subject in this store. A total quoted as the subject's should be "
            f"the first figure, not the sum"
        )


def _fold(address: str) -> str:
    """Normalise without knowing the chain.

    `.lower()` is right on EVM hex and destroys base58 and bech32 --- silently,
    by making two different addresses compare equal. `fold_if_hex` folds only
    what is unambiguously 42-character hex and returns everything else as given.
    """
    return fold_if_hex(address.strip())


def _key(transfer: Any, field_name: str) -> str:
    value = getattr(transfer, field_name, None)
    if value is None:
        return ""
    raw = getattr(value, "key", None) or getattr(value, "raw", None) or value
    # `_fold`, not `.lower()`. The two must agree: the search normalises the
    # subject with `_fold` and compares it against endpoints normalised here, so
    # lowercasing on this side alone meant a Solana, Sui or Bitcoin address
    # never matched itself. Half-fixing a pair is worse than leaving both
    # wrong --- `.lower()` on both sides at least agreed with itself.
    return _fold(str(raw))


def _funders(transfers: list[Any]) -> dict[str, str]:
    """Who paid each address first, by timestamp.

    First *seen in this store*, which is a weaker statement than first ever and
    is the only one the data supports. Used for the `co_funded` bucket, which is
    reported separately for that reason.
    """
    earliest: dict[str, tuple[Any, str]] = {}
    for transfer in transfers:
        when = getattr(transfer, "timestamp", None)
        sender, recipient = _key(transfer, "sender"), _key(transfer, "recipient")
        if when is None or not sender or not recipient:
            continue
        seen = earliest.get(recipient)
        if seen is None or when < seen[0]:
            earliest[recipient] = (when, sender)
    return {address: funder for address, (_when, funder) in earliest.items()}


def contributors(
    transfers: list[Any],
    target: str,
    subject: str,
    *,
    max_hops: int = 4,
    chain: Any = None,
) -> Inflow:
    """Break down what reached ``target``, by who sent it and how they relate.

    ``max_hops`` bounds the reachability search and therefore bounds the
    ``reachable`` bucket: a contributor connected to the subject by five hops,
    searched to four, lands in ``unlinked``. That bound travels with the result
    so nothing reads "no link" without reading how far anybody looked.
    """
    goal = _fold(target)
    origin = _fold(subject)

    amounts: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    symbols: dict[str, str] = {}
    places: dict[str, int | None] = {}
    for transfer in transfers:
        if _key(transfer, "recipient") != goal:
            continue
        sender = _key(transfer, "sender")
        if not sender:
            continue
        amount = getattr(transfer, "amount", None)
        amounts[sender] += int(getattr(amount, "raw", 0) or 0)
        counts[sender] += 1
        symbols.setdefault(sender, str(getattr(amount, "symbol", "") or ""))
        if sender not in places:
            found_places = getattr(amount, "decimals", None)
            places[sender] = int(found_places) if found_places is not None else None

    funders = _funders(transfers)
    subject_funder = funders.get(origin, "")

    found: list[Contribution] = []
    for sender, amount in amounts.items():
        if sender == origin:
            link, detail = Link.SELF, "the subject itself"
        else:
            routes, _notes = find_routes(
                transfers, origin, sender, max_hops=max_hops, max_routes=1, chain=chain
            )
            if routes:
                path = " -> ".join(a[:10] + "…" for a in routes[0].addresses)
                link, detail = Link.REACHABLE, f"reachable from the subject: {path}"
            elif subject_funder and funders.get(sender) == subject_funder:
                link = Link.CO_FUNDED
                detail = (
                    f"first funded by {subject_funder}, as the subject was. An "
                    f"exchange funds thousands of unrelated customers, so this "
                    f"is much weaker than a route"
                )
            else:
                link = Link.UNLINKED
                detail = (
                    f"no time-respecting route from the subject within "
                    f"{max_hops} hops, and no shared first funder, in this store"
                )
        found.append(
            Contribution(
                address=sender,
                amount=amount,
                transfers=counts[sender],
                symbol=symbols.get(sender, ""),
                decimals=places.get(sender),
                link=link,
                detail=detail,
            )
        )

    # Largest first: the contributor that most changes the total is the one
    # whose classification most needs checking.
    found.sort(key=lambda c: (-c.amount, c.address))
    return Inflow(
        target=goal,
        subject=origin,
        contributions=found,
        hops_searched=max_hops,
        transfers_searched=len(transfers),
    )


def findings(inflow: Inflow) -> list[Finding]:
    """The decomposition, with the unattributable part named rather than netted."""
    if not inflow.contributions:
        return [
            Finding(
                title=f"nothing reached {inflow.target[:10]}… in this store",
                severity=Severity.INFO,
                detail=(
                    f"No inbound transfers over {inflow.transfers_searched} "
                    f"searched. That is a statement about the store, not about "
                    f"the address."
                ),
                data={"target": inflow.target, "contributors": 0},
            )
        ]

    unlinked = [c for c in inflow.contributions if c.link == Link.UNLINKED]
    co_funded = [c for c in inflow.contributions if c.link == Link.CO_FUNDED]
    out = [
        Finding(
            title=(
                f"{inflow.show(inflow.total)} reached {inflow.target[:10]}… from "
                f"{len(inflow.contributions)} address(es)"
            ),
            # IMPORTANT rather than CRITICAL: nothing here is wrong yet. It
            # becomes wrong when somebody quotes the sum as the subject's, and
            # this is the finding that stops that.
            severity=Severity.IMPORTANT if unlinked or co_funded else Severity.INFO,
            detail=(
                f"{inflow.show(inflow.attributable)} is the subject's own or "
                f"traceable to it within {inflow.hops_searched} hops.\n"
                f"{inflow.show(inflow.unlinked)} came from {len(unlinked)} "
                f"address(es) with no link to the subject in this store.\n"
                f"\n"
                f"A deposit address is a destination, not a private channel: "
                f"anybody may pay it. Quote the first figure as the subject's, "
                f"not the sum.\n"
                f"\n"
                f"Nothing is subtracted here. Whether an unlinked contributor is "
                f"a stranger or a hop nobody fetched is a judgement with context "
                f"this code does not have."
            ),
            data={
                "target": inflow.target,
                "subject": inflow.subject,
                "total": str(inflow.total),
                "attributable": str(inflow.attributable),
                "unlinked": str(inflow.unlinked),
                "hops_searched": inflow.hops_searched,
                "symbol": inflow.symbol,
            },
        )
    ]

    for contribution in unlinked + co_funded:
        out.append(
            Finding(
                title=(
                    f"{inflow.show(contribution.amount)} from "
                    f"{contribution.address[:10]}…, {contribution.link}"
                ),
                severity=Severity.NOTABLE,
                detail=(
                    f"{contribution.transfers} transfer(s).\n"
                    f"  - {contribution.detail}\n"
                    f"  - 'unlinked' means no link was found, not that none "
                    f"exists. Backtrack this address before excluding it: in the "
                    f"case this check comes from, the third party had 122 "
                    f"transactions of its own and was funded from an unrelated "
                    f"500 ETH, which is what settled it."
                ),
                data={
                    "address": contribution.address,
                    "amount": str(contribution.amount),
                    "transfers": contribution.transfers,
                    "link": contribution.link,
                },
            )
        )
    return out


class ContributorsAnalyzer(Analyzer):
    """Who paid into an address, and which of them relate to the subject."""

    name = "contributors"
    description = "split an address's inflow by who sent it and how they relate"
    requires = Capability.ASSET_TRANSFERS

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, self.requires))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        subject: str = "",
        max_hops: int = 4,
        max_expand: int = 60,
        **_: Any,
    ) -> Result:
        if not address or not subject:
            raise ValueError(
                "contributors analysis needs an `address` whose inflow to split "
                "and a `subject` to relate it to"
            )
        started = datetime.now(timezone.utc)
        target = address_key(ctx.chain, address)
        origin = address_key(ctx.chain, subject)
        per_node = ctx.limit("per_node", 1000)

        # Both ends, then outward. Reading only the target's and subject's
        # histories gives the payers and one hop either side --- and then
        # `max_hops` promises to search four, over data that only covers one.
        # A contributor two hops from the subject was reported `unlinked` while
        # the parameter said it had been looked for, which is the worst of both:
        # a claim about a search that did not happen.
        #
        # So the frontier expands until it has the depth `max_hops` claims, or
        # until `max_expand` stops it --- and when it is stopped, that is said,
        # because "unlinked" then means something narrower again.
        rows: list[Any] = []
        notes: list[str] = []
        seen: set[str] = set()
        frontier = [target, origin]
        for _depth in range(max(1, max_hops)):
            if len(seen) >= max_expand:
                notes.append(
                    f"stopped after reading {len(seen)} addresses (max_expand="
                    f"{max_expand}). A contributor linked to the subject only "
                    f"through an address beyond that is reported unlinked, "
                    f"because nobody looked --- not because there is no link"
                )
                break
            nxt: list[str] = []
            for who in frontier:
                if who in seen or len(seen) >= max_expand:
                    continue
                seen.add(who)
                try:
                    fetched, source_notes = history_of(ctx, _read(ctx, who, per_node))
                except ProviderError as exc:
                    # Too busy to page is itself the answer: a service. Recorded
                    # rather than fatal, exactly as `route` does.
                    notes.append(
                        f"{who} has more history than one page holds, so it was "
                        f"not expanded through ({exc})"
                    )
                    continue
                notes.extend(source_notes)
                rows.extend(fetched)
                for row in fetched:
                    for end in ("sender", "recipient"):
                        other = _key(row, end)
                        if other and other not in seen:
                            nxt.append(other)
            # Least-connected first, so the budget is spent on addresses that
            # might be a step rather than on an exchange's counterparties.
            counts: dict[str, int] = {}
            for candidate in nxt:
                counts[candidate] = counts.get(candidate, 0) + 1
            frontier = sorted(dict.fromkeys(nxt), key=lambda a: counts.get(a, 0))

        inflow = contributors(rows, target, origin, max_hops=max_hops, chain=ctx.chain)
        if inflow.unlinked:
            notes.append(
                f"{inflow.show(inflow.unlinked)} of the inflow has no link to the "
                f"subject in what was read. Do not quote the sum as the subject's "
                f"without deciding about that portion"
            )
        return Result(
            analyzer=self.name,
            findings=tuple(findings(inflow)),
            warnings=tuple(dict.fromkeys(notes)),
            evidence=ctx.evidence(),
            params={
                "address": address,
                "subject": subject,
                "chain": str(ctx.chain),
                "max_hops": max_hops,
                "max_expand": max_expand,
                "addresses_read": len(seen),
                "per_node": per_node,
            },
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )


def _read(ctx: Context, address: str, limit: int) -> Any:
    """A reader bound to one address, not to the loop variable."""

    def fetch(provider: Any) -> Any:
        return provider.asset_transfers(ctx.chain, address, direction="all", limit=limit)

    return fetch
