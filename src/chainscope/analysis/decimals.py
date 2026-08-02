"""Token decimals are not a constant, and treating them as one is a factor of 10^n.

Every amount in this package is a raw integer plus a decimals value, and
`decimals` is read from the token. The unstated assumption is that it never
changes. For an immutable ERC-20 that holds; for a proxy-upgradeable one it
does not, and a contest task turns on exactly this --- it asks for a token's
*historical* decimals, because the current value is the wrong one.

The failure is silent and enormous. A token that moved from 8 to 18 decimals
renders every earlier transfer ten billion times too small. Small enough to
read as dust and be skipped, which is worse than an obvious error: the
investigator does not see a wrong number, they see nothing worth looking at.

So decimals are resolved **at a block**, and a lookup that cannot establish the
value at that block **refuses** rather than falling back to the current one.
Falling back is the bug: it produces a plausible number with no indication that
it describes a different point in time.

The same reasoning appears in field notes on a token-locking exploit: rebuild
the true state at each moment with a historical ``eth_call``, and do not trust
events alone. An event saying 1e11 moved while ``balanceOf`` says nothing did
is not a contradiction to resolve --- the difference is the finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = [
    "DecimalsUnknown",
    "TokenDecimals",
    "format_at",
    "resolve_at",
]


class DecimalsUnknown(RuntimeError):
    """The decimals for this token at this block could not be established.

    A distinct type because the caller must not treat it as "use the default".
    An amount rendered with the wrong decimals is off by a power of ten and
    carries no sign of it, so refusing is the only honest outcome.
    """


@dataclass
class TokenDecimals:
    """Decimals for one token, as they were at a given block.

    Readings are cached by block because a historical ``eth_call`` is an
    archive query and archive access is the scarcest capability most
    deployments have.

    **A change is a finding, not a nuisance.** When two readings disagree the
    token was upgraded between them, and every amount recorded in that interval
    needs re-checking. The history is kept rather than collapsed to a latest
    value, so the change stays visible.
    """

    token: str
    readings: dict[int, int] = field(default_factory=dict)
    """Block to decimals. Sparse: only blocks actually queried."""

    def observe(self, block: int, value: int) -> None:
        if value < 0 or value > 77:
            # 77 is where 10**n stops fitting a uint256. Beyond it the value is
            # not a decimals field, and accepting it would let a hostile or
            # misparsed token make every amount zero.
            raise ValueError(f"{value} is not a plausible decimals value")
        self.readings[block] = value

    @property
    def changed(self) -> bool:
        return len(set(self.readings.values())) > 1

    def at(self, block: int) -> int:
        """Decimals in force at ``block``.

        Uses the nearest reading **at or before** that block, which is what
        "in force" means: a value read later says nothing about earlier, since
        the upgrade may have happened in between.

        Raises when the only readings are later. That case looks harmless and
        is the dangerous one --- the answer would be a real value from the
        wrong era.
        """
        earlier = [b for b in self.readings if b <= block]
        if not earlier:
            known = min(self.readings, default=None)
            raise DecimalsUnknown(
                f"{self.token} has no decimals reading at or before block {block}"
                + (
                    f"; the earliest is block {known}, which is after it. A later "
                    f"reading cannot establish an earlier value, because the "
                    f"upgrade may have happened in between."
                    if known is not None
                    else ". Read decimals() at a historical block first."
                )
            )
        return self.readings[max(earlier)]

    def summary(self) -> str:
        if not self.readings:
            return f"{self.token}: no decimals readings."
        if not self.changed:
            value = next(iter(self.readings.values()))
            return (
                f"{self.token}: {value} decimals across {len(self.readings)} "
                f"reading(s). No change observed --- which is not the same as no "
                f"change, since only the blocks queried were checked."
            )
        pairs = ", ".join(f"block {b}: {v}" for b, v in sorted(self.readings.items()))
        return (
            f"{self.token}: decimals CHANGED ({pairs}). Every amount recorded "
            f"between those blocks needs re-checking --- rendering one with the "
            f"wrong value is off by a power of ten and looks like a normal number."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "readings": dict(sorted(self.readings.items())),
            "changed": self.changed,
            "summary": self.summary(),
        }


def resolve_at(
    token: str,
    block: int,
    call_decimals: Any,
    *,
    cache: TokenDecimals | None = None,
) -> tuple[int, TokenDecimals]:
    """Read a token's decimals at a block, caching the result.

    ``call_decimals`` takes ``(token, block)`` and returns the value, which is
    an ``eth_call`` against a historical block --- an archive query. Injected so
    the same function works against a node, a cassette, or readings somebody
    already collected.

    A provider that fails is not evidence of any particular decimals value, so
    the failure propagates as :class:`DecimalsUnknown` rather than defaulting.
    """
    known = cache or TokenDecimals(token=token.lower())
    try:
        return known.at(block), known
    except DecimalsUnknown:
        pass

    try:
        value = int(call_decimals(token, block))
    except DecimalsUnknown:
        raise
    except Exception as exc:
        raise DecimalsUnknown(
            f"could not read decimals for {token} at block {block}: {exc}. "
            f"Not defaulting: an amount rendered with the wrong decimals is off "
            f"by a power of ten and carries no sign of it."
        ) from exc

    known.observe(block, value)
    return value, known


def format_at(raw: int, token: str, block: int, known: TokenDecimals) -> Decimal:
    """Render a raw amount using the decimals in force at ``block``.

    Decimal rather than float: wei-scale values exceed float64's exact range,
    and a figure that prints as 19.999999999999996 invites the reader to
    distrust arithmetic that was correct.
    """
    return Decimal(raw) / (Decimal(10) ** known.at(block))
