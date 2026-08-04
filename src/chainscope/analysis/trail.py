"""What actually moved, in and out, in order --- with the noise named.

The complaint this exists for: *"it is hard to find the real attack path
through the addresses, there is too much of it."* Correct, and the numbers say
how correct. The LpdFi attacker's address carried **85 transfers, of which six
were real**. The other seventy-nine were zero-value address-poisoning dust. A
graph drawn over all eighty-five is not a picture of a theft; it is a picture
of a theft buried in someone else's spam campaign.

`contributors` already answers *who* paid in and how they relate. This answers
the question underneath it: **which movements are worth looking at at all**,
and it answers it in both directions, because who funded an address is
frequently the more useful half. In this case it was: the attacker was staked
116,495 USDC by an address two thousand blocks before the exploit, and every
manual trace that started at the exploit block missed it.

**Two things are set aside, and only two.** Both are arithmetic or evidenced,
never a judgement about size:

*Zero-value transfers.* A transfer of nothing moves nothing. It is a real log
from a real contract --- anyone may call `transfer(victim, 0)` --- and that is
exactly why it is used: it plants an address in somebody's history where they
will copy it. There is nothing to trace, so it does not belong on a path.

*Forged assets.* A token whose symbol imitates a canonical one, established by
`impersonation` against the canonical contract and UTS #39 rather than by
suspicion. Its "transfers" are whatever its author chose to emit, including the
`from` address, so a path built through one is a path through a fiction.

**Nothing is dropped for being small.** Dust thresholds are where a filter
starts deciding the answer: a 100 USDC test payment before a 689,429 USDC
payout is 1.45e-4 of the flow and is the single most incriminating movement in
this case. Small movements are marked `minor` and kept --- `steps` holds
everything, `significant` folds --- so a reader can collapse them and a
`probing` analysis can still find them. See `_DUST_SHARE` for where the line
sits and for the first value I chose, which folded that very payment.

**And the discards are counted and explained in the result.** A path that
silently became legible is a path somebody trusts for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..chains import address_key
from ..core.chainid import ChainId

__all__ = [
    "SERVICE_DEGREE",
    "Direction",
    "SetAside",
    "Step",
    "Trail",
    "trail",
]

#: Distinct counterparties above which an address behaves like a service.
#:
#: A shape, never an identification --- the result says "behaves like", and a
#: reader who needs the name still has to find it. But the shape is enough to
#: decide the only thing this module does with it, which is stop.
#:
#: Measured on the address this rule was added for: `0xb92fe925…4fff4f` paid
#: into the LpdFi attacker's funder and received the proceeds back, and I read
#: that closed loop as one party's treasury. It is not. In the *fragment* the
#: case happened to hold it has 1,127 distinct senders and 1,565 distinct
#: recipients across 176 assets. A private wallet does not receive a hundred
#: and seventy-six different tokens from a thousand addresses; value arriving
#: at and leaving a service says nothing whatever about a single owner.
#:
#: Forty is far below that and far above a person. It is deliberately not
#: tuned finely: the cost of calling a busy individual a service is a trail
#: that stops one hop early and says so, and the cost of the reverse is the
#: omnibus error --- attributing a thousand strangers' money to one of them.
SERVICE_DEGREE = 40


#: Below this share of the largest movement in the same asset, a step is folded.
#:
#: Measured on the LpdFi case rather than picked, and the first value picked
#: was wrong in a way worth recording. At 0.001 it folded the **100 USDC test
#: payment** --- the movement this module's own docstring calls the single most
#: incriminating one --- because 100 against a 689,429 payout is 1.45e-4.
#:
#:     payout   689,429.793148 USDC   share 1.0
#:     test         100.000000 USDC   share 1.45e-04
#:     dust           0.000069 USDC   share 1.00e-10
#:
#: Six orders of magnitude separate a deliberate test payment from poisoning
#: dust, so the line sits between them with two orders of room on each side. A
#: threshold that cannot tell those apart is not measuring smallness, it is
#: measuring nothing.
_DUST_SHARE = Decimal("1e-6")


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class SetAside(str, Enum):
    """Why a transfer is not on the path. Never "it looked small"."""

    ZERO = "zero-value"
    """Moves nothing. The address-poisoning technique that uses the *real*
    token contract, so neither a symbol check nor a contract check sees it ---
    only the amount does."""

    FORGED_ASSET = "forged-asset"
    """The token imitates a canonical one. Its logs say whatever its author
    chose, including who sent them."""


@dataclass(frozen=True, slots=True)
class Step:
    """One movement worth reading."""

    direction: Direction
    counterparty: str
    amount_raw: int
    symbol: str
    decimals: int
    asset: str | None
    block: int | None
    at: datetime | None
    tx: str
    boundary: bool = False
    """This counterparty behaves like a service, so the trail stops here.

    Not an accusation and not an identification. Money entering a router, a
    bridge or an exchange is pooled with everybody else's, so following it
    onward produces a path that is an artefact of shared infrastructure. Every
    other module here already stops at one --- `linked_holders` treats a
    service as a boundary rather than an edge, taint stops at a terminal
    category --- and this one did not, which is how a closed loop through a
    router got read as one party's treasury."""

    minor: bool = False
    """Small relative to the largest step on this trail. **Kept**, because the
    smallest movement in a case is routinely the one that proves intent --- a
    test payment before the real one is 0.01% of the flow and the whole
    argument."""

    @property
    def human(self) -> Decimal:
        if self.decimals <= 0:
            return Decimal(self.amount_raw)
        return Decimal(self.amount_raw) / (Decimal(10) ** self.decimals)


@dataclass(frozen=True, slots=True)
class Trail:
    """The address's material movements, in and out, oldest first."""

    address: str
    chain: ChainId | None
    steps: tuple[Step, ...] = ()
    set_aside: dict[SetAside, int] = field(default_factory=dict)
    forged_assets: tuple[str, ...] = ()
    """Contracts the impersonation check named. Listed so the reader can check
    the call rather than take it."""

    boundaries: tuple[str, ...] = ()
    """Counterparties that behave like services. The trail reaches them and
    stops; what happened to the money inside is not visible from here and is
    not guessed at."""

    @property
    def stops_at_a_service(self) -> bool:
        return bool(self.boundaries)

    @property
    def funding(self) -> tuple[Step, ...]:
        """What came in. Who paid this address, oldest first."""
        return tuple(s for s in self.steps if s.direction is Direction.IN)

    @property
    def onward(self) -> tuple[Step, ...]:
        """What went out."""
        return tuple(s for s in self.steps if s.direction is Direction.OUT)

    @property
    def significant(self) -> tuple[Step, ...]:
        """The steps a reader wants first: everything not marked `minor`.

        Folding rather than filtering, and the difference matters. The dust in
        the LpdFi case is not zero --- it is 0.0000689 USDC against a real
        689,429.79, and the digits are *chosen to match*: `68942979314844`
        beside `689429793148448987344168` reads as the same number in a wallet
        that truncates. So it cannot be caught by a zero check and must not be
        deleted, because an amount engineered to look like the real one is
        itself evidence of who was targeted and when.

        It is set to one side and counted. `steps` still holds everything.
        """
        return tuple(s for s in self.steps if not s.minor)

    @property
    def considered(self) -> int:
        return len(self.steps) + sum(self.set_aside.values())

    def summary(self) -> str:
        """One line stating the ratio, because the ratio is the finding.

        A reader told "six movements" and not told that seventy-nine were
        removed has been handed a cleaned-up picture with no way to judge the
        cleaning.
        """
        removed = sum(self.set_aside.values())
        minor = len(self.steps) - len(self.significant)
        parts = [f"{len(self.significant)} of {self.considered} movement(s) stand out."]
        if minor:
            parts.append(
                f"{minor} more are minor --- kept and folded, not deleted, "
                f"because an amount engineered to resemble the real one is "
                f"evidence rather than noise."
            )
        if removed:
            why = ", ".join(
                f"{n} {reason.value}" for reason, n in sorted(self.set_aside.items())
            )
            parts.append(f"{removed} set aside ({why}).")
        if self.boundaries:
            parts.append(
                f"{len(self.boundaries)} counterparty(ies) behave like services "
                f"and the trail stops at them --- their funds are pooled with "
                f"everybody else's, so following further would trace shared "
                f"infrastructure rather than this money."
            )
        return " ".join(parts)


def _minor_below(steps: Sequence[tuple[str, int]], share: Decimal) -> dict[str, int]:
    """Per asset, the amount under which a step is marked minor.

    Per asset rather than globally, because a threshold shared across assets
    compares raw units of things with different decimals --- 1 USDC and 1 wei
    are both "1" and one of them is a millionth of a cent.
    """
    largest: dict[str, int] = {}
    for asset, raw in steps:
        largest[asset] = max(largest.get(asset, 0), raw)
    return {asset: int(Decimal(top) * share) for asset, top in largest.items()}


def _service_shaped(rows: list[Any], key: Any, degree: int) -> set[str]:
    """Counterparties with more distinct counterparties than a person has.

    Counted over the transfers in hand, so it *under*-reports --- an address
    the case has barely seen looks quiet. That direction is the safe one: a
    missed boundary shows as a trail that continued, which a reader can see and
    argue with, where a false boundary shows as a trail that stopped, which
    looks identical to the money stopping.
    """
    peers: dict[str, set[str]] = {}
    for row in rows:
        sender = getattr(row, "sender", None)
        recipient = getattr(row, "recipient", None)
        if sender is None or recipient is None:
            continue
        a, b = key(sender.raw), key(recipient.raw)
        peers.setdefault(a, set()).add(b)
        peers.setdefault(b, set()).add(a)
    return {who for who, seen in peers.items() if len(seen) >= degree}


def trail(
    transfers: Iterable[Any],
    address: str,
    *,
    chain: ChainId | None = None,
    minor_share: Decimal = _DUST_SHARE,
    service_degree: int = SERVICE_DEGREE,
) -> Trail:
    """Material movements touching ``address``, both directions, oldest first.

    ``minor_share`` only *marks*; it never removes. See the module docstring
    for why a dust threshold that removes is a filter deciding the answer.
    """
    rows = list(transfers)
    forged = _forged_assets(rows, chain)
    key = (lambda a: address_key(chain, a)) if chain is not None else (lambda a: a.strip())
    wanted = key(address)

    services = _service_shaped(rows, key, service_degree) - {wanted}
    aside: dict[SetAside, int] = {}
    kept: list[dict[str, Any]] = []
    for row in rows:
        sender = getattr(row, "sender", None)
        recipient = getattr(row, "recipient", None)
        amount = getattr(row, "amount", None)
        if amount is None or sender is None or recipient is None:
            continue
        here_out = key(sender.raw) == wanted
        here_in = key(recipient.raw) == wanted
        if not (here_out or here_in):
            continue

        asset = getattr(row, "asset", None)
        asset_key = asset.raw.lower() if asset else ""
        if asset_key and asset_key in forged:
            aside[SetAside.FORGED_ASSET] = aside.get(SetAside.FORGED_ASSET, 0) + 1
            continue
        if amount.raw <= 0:
            aside[SetAside.ZERO] = aside.get(SetAside.ZERO, 0) + 1
            continue

        kept.append(
            {
                "row": row,
                "direction": Direction.OUT if here_out else Direction.IN,
                "counterparty": (recipient if here_out else sender).raw,
                "asset_key": asset_key,
            }
        )

    floors = _minor_below([(k["asset_key"], k["row"].amount.raw) for k in kept], minor_share)
    steps = [
        Step(
            direction=k["direction"],
            counterparty=k["counterparty"],
            amount_raw=k["row"].amount.raw,
            symbol=k["row"].amount.symbol or "",
            decimals=k["row"].amount.decimals,
            asset=k["asset_key"] or None,
            block=getattr(k["row"], "block", None),
            at=getattr(k["row"], "timestamp", None),
            tx=str(getattr(getattr(k["row"], "tx", None), "hash", "")),
            minor=k["row"].amount.raw < floors.get(k["asset_key"], 0),
            boundary=key(k["counterparty"]) in services,
        )
        for k in kept
    ]
    steps.sort(key=lambda s: (s.block or 0, s.tx))
    return Trail(
        address=address,
        chain=chain,
        steps=tuple(steps),
        set_aside=aside,
        forged_assets=tuple(sorted(forged)),
        boundaries=tuple(sorted({s.counterparty for s in steps if s.boundary})),
    )


def _forged_assets(rows: list[Any], chain: ChainId | None) -> set[str]:
    """Contracts the impersonation check calls lookalikes.

    Delegated rather than reimplemented: the canonical-contract comparison and
    the UTS #39 skeleton live in one place and this is not a second opinion on
    them. A failure to load the registry leaves the set empty, which keeps
    every transfer on the trail --- the safe direction, since a forged asset
    left in is visible and a real one removed is not.
    """
    try:
        from .impersonation import Verdict, inspect_assets

        # FORGED and LOOKALIKE only. `is_impersonation` also covers
        # UNKNOWN_SCRIPT, which says "this symbol mixes scripts" and *not*
        # "this imitates something" --- a legitimate token named in one
        # non-Latin script would land there, and removing its transfers from a
        # trail on that basis would be the filter deciding the answer. The two
        # kept are the ones with a canonical contract to compare against.
        return {
            found.contract.lower()
            for found in inspect_assets(rows, chain)
            if found.contract and found.verdict in (Verdict.FORGED, Verdict.LOOKALIKE)
        }
    except Exception:
        return set()
