"""Revenue splits: who takes a fixed cut, and who is just passing through.

A contract that distributes value to several recipients in one transaction is
running a business model, and the model is legible in the arithmetic. From a
real drainer-as-a-service case: each theft paid an affiliate, an operator, and
a relayer, in one transaction, out of one contract.

**The affiliate changes every time. The operator's percentage does not.** That
asymmetry is the whole technique. A recipient taking 20.00% of eighty different
distributions is not a participant in eighty coincidences --- it is a party to
an agreement, and the agreement is what identifies the role. A recipient whose
share varies is doing something else: reimbursing gas, taking what is left, or
being paid for that job rather than that percentage.

So this measures **share stability across transactions**, not share size in one.
One transaction tells you who was paid. Many tell you what they are.

Two things it deliberately does not do.

It does not name roles. "Operator" and "affiliate" are a reading of the case,
and a function that printed them would be asserting a business structure from
arithmetic. What comes back is: this address took a stable *n*% across *k*
distributions, and this one did not.

It does not treat a round number as proof. A stable 20% is strong because
20.00% recurring is unlikely by chance; a stable 19.37% is *equally* strong
statistically and rounder numbers are not more meaningful. Roundness is
reported because a human reader finds it persuasive, and separated from the
stability measure so it cannot quietly do the work.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "MIN_DISTRIBUTIONS",
    "MIN_MEANINGFUL_BPS",
    "STABLE_TOLERANCE",
    "Distribution",
    "Recipient",
    "analyse_splits",
]

#: How many distributions before a share is called stable.
#:
#: Four, because with three a fixed share is not much of a claim: two
#: recipients splitting evenly hit the same percentage every time for reasons
#: that have nothing to do with an agreement. Four separates a party to a deal
#: from an artefact of small numbers, and the count travels with the result so
#: a reader can apply their own floor.
MIN_DISTRIBUTIONS = 4

#: How much a share may vary, as a fraction of the share itself.
#:
#: **Relative, not absolute, and the first version got this wrong.** A flat
#: 25-basis-point tolerance called a gas rebate stable: its shares were 10 to
#: 25 bps of the total, a spread of 15, comfortably inside the allowance. But
#: that rebate had *more than doubled* between the smallest and the largest
#: distribution --- the opposite of a fixed cut.
#:
#: The error is scale. A 20% cut varying by 25 bps varies by 0.125% of itself;
#: a 0.1% rebate varying by 15 bps varies by 150% of itself. Only the second
#: number describes what a reader means by "fixed".
#:
#: Five percent of the share, which absorbs the basis point or two that integer
#: division on wei introduces and rejects anything that tracks something other
#: than the total.
STABLE_TOLERANCE = Decimal("0.05")

#: Floor for the relative test, in basis points. Below this a share is small
#: enough that rounding dominates, and a relative measure on it is noise.
MIN_MEANINGFUL_BPS = 5


@dataclass(frozen=True, slots=True)
class Distribution:
    """One transaction's outgoing value, split among recipients."""

    tx: str
    payouts: dict[str, int]
    """Recipient to raw amount. Only positive outgoing value."""

    chain: str | None = None
    """Which chain these addresses are on, when the caller knows.

    Decides how two spellings of a recipient compare. Without it the fallback
    folds only EVM-shaped hex --- see :func:`_fold`.
    """

    @property
    def total(self) -> int:
        return sum(self.payouts.values())

    def share_bps(self, address: str) -> int:
        """This recipient's cut, in basis points of the whole distribution.

        The lookup lowercases *both* sides. It used to lowercase only the
        query, so a distribution built from checksummed addresses --- which is
        what every EVM provider returns --- matched nothing and reported every
        recipient as 0 bps. A revenue split that finds no splits looks exactly
        like an address that does not take a cut.
        """
        total = self.total
        if total <= 0:
            return 0
        return (self._by_key().get(_fold(self.chain, address), 0) * 10_000) // total

    def _by_key(self) -> dict[str, int]:
        """Payouts keyed comparably, summing any addresses that collapse.

        Two spellings of one address are one recipient, and adding them is the
        only answer that keeps `total` and the shares consistent.
        """
        folded: dict[str, int] = {}
        for address, amount in self.payouts.items():
            key = _fold(self.chain, address)
            folded[key] = folded.get(key, 0) + amount
        return folded


def _fold(chain: str | None, address: str) -> str:
    """How two spellings of one recipient compare.

    With a chain, the adapter decides --- the only correct answer, since the
    rule is per-ecosystem.

    Without one, only **EVM-shaped hex** folds: 42 characters starting `0x`,
    where case is a checksum and two spellings are one address. Everything else
    is compared as written, because base58 case is part of the value and
    folding it merges two people's addresses into one recipient.

    A chainless fallback of `.lower()` was what produced both halves of this
    module's bug: shares of 0 bps on checksummed EVM input, and --- had anyone
    run it on Solana --- two accounts reported as one.
    """
    if chain:
        from ..chains import address_key

        return address_key(chain, address)
    from ..chains import fold_if_hex

    return fold_if_hex(address)


@dataclass
class Recipient:
    """One address's history across a set of distributions."""

    address: str
    shares_bps: list[int] = field(default_factory=list)
    received: int = 0
    appearances: int = 0

    @property
    def median_bps(self) -> int:
        return int(statistics.median(self.shares_bps)) if self.shares_bps else 0

    @property
    def spread_bps(self) -> int:
        """Widest gap between the shares taken. Zero means an exact fixed cut."""
        return max(self.shares_bps) - min(self.shares_bps) if self.shares_bps else 0

    @property
    def is_stable(self) -> bool:
        """Whether the share held steady *relative to itself*.

        Measured against the median rather than as an absolute width, because
        a tolerance that works for a 20% cut is meaningless for a 0.1% rebate.
        """
        if self.appearances < MIN_DISTRIBUTIONS:
            return False
        median = self.median_bps
        if median < MIN_MEANINGFUL_BPS:
            # Too small for the relative test to mean anything: at this size
            # one wei of rounding moves the figure by a large fraction.
            return False
        return Decimal(self.spread_bps) / Decimal(median) <= STABLE_TOLERANCE

    @property
    def is_round(self) -> bool:
        """Whether the median lands on a whole percent.

        Reported, never used to decide. A stable 19.37% is exactly as unlikely
        by chance as a stable 20.00%; only a human reader finds the second more
        convincing, and letting that do the work would be dressing intuition as
        measurement.
        """
        return self.median_bps % 100 == 0

    @property
    def percent(self) -> Decimal:
        # Decimal, not float: a share printed as 19.999999999999996 invites the
        # reader to distrust the arithmetic, and this figure gets quoted.
        return Decimal(self.median_bps) / Decimal(100)

    def summary(self) -> str:
        if not self.is_stable:
            if self.appearances < MIN_DISTRIBUTIONS:
                return (
                    f"{self.address} appears in {self.appearances} distribution(s), "
                    f"below the {MIN_DISTRIBUTIONS} needed before a share is called "
                    f"stable. Too few to tell an agreement from a coincidence."
                )
            low, high = min(self.shares_bps), max(self.shares_bps)
            factor = f"{high / low:.1f}x" if low else "an unbounded factor"
            return (
                f"{self.address} took between {low / 100:.2f}% and {high / 100:.2f}% "
                f"across {self.appearances} distributions --- a {factor} range. A "
                f"share that varies with something other than the total is somebody "
                f"being paid for a job rather than to a rate: a gas rebate, or "
                f"whatever was left over."
            )
        return (
            f"{self.address} took {self.percent:.2f}% of every distribution across "
            f"{self.appearances} of them, varying by {self.spread_bps / 100:.2f}%. A "
            f"share that fixed is an agreement, not a coincidence. It does not say "
            f"what the agreement was, or that this address is the party that made it."
        )

    def attribution(self, chain: ChainId | None = None) -> Attribution | None:
        if not self.is_stable:
            return None
        return Attribution(
            label=f"takes a fixed {self.percent:.2f}% cut",
            category=Category.SERVICE,
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            source="chainscope revenue-split analysis",
            address=self.address,
            chain=chain,
            rationale=self.summary(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "appearances": self.appearances,
            "median_percent": str(self.percent),
            "spread_bps": self.spread_bps,
            "stable": self.is_stable,
            "round_percent": self.is_round,
            "received_raw": str(self.received),
        }


def analyse_splits(distributions: list[Distribution]) -> list[Recipient]:
    """Measure how stable each recipient's share is across distributions.

    Sorted with the stable shares first, largest cut leading, because that is
    the order a reader wants: the parties to an agreement before the ones
    passing through.

    Recipients absent from a distribution do not score zero for it. A share is
    only defined where value was actually split, and counting an absence as 0%
    would make an occasional participant look wildly variable when it is simply
    not always involved.
    """
    recipients: dict[str, Recipient] = {}
    for dist in distributions:
        if dist.total <= 0:
            continue
        for address, amount in dist.payouts.items():
            if amount <= 0:
                continue
            key = _fold(dist.chain, address)
            # Reported as written. Lowercasing the *output* names a different
            # account on Solana, Sui and Bitcoin --- possibly one that does not
            # exist --- in a finding about who takes a cut.
            entry = recipients.setdefault(key, Recipient(address=address))
            entry.shares_bps.append(dist.share_bps(key))
            entry.received += amount
            entry.appearances += 1

    return sorted(
        recipients.values(),
        key=lambda r: (not r.is_stable, -r.median_bps, r.address),
    )
