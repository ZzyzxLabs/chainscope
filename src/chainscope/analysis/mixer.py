"""Mixer deposit-to-withdrawal correlation, and the anonymity set that decides it.

A mixer breaks the on-chain link by construction: a withdrawal proves knowledge
of *some* deposit's secret and says nothing about which. No amount of chain
analysis undoes that. What is attackable is **operator behaviour** --- someone
who deposits and then withdraws a few minutes later, repeatedly, leaves a
timing pattern the cryptography was never asked to hide.

Field notes from a real trace record the clean version: thirteen deposits into
one pool, and for each of them the pool's *next* withdrawal was the matching
one, twelve to thirty-nine blocks later. Thirteen for thirteen, no gaps, no
double-claims.

**The rule is worthless without the number beside it.** "The next withdrawal
after mine" is a strong claim in a pool where nothing else happened in that
window and a meaningless one in a pool with forty withdrawals a minute --- and
the *procedure is identical in both cases*. That makes this the sharpest
example in the package of a technique that produces a confident answer whether
or not it has any basis, so the anonymity set travels with every match and
decides its confidence rather than decorating it.

Precision measured against known ground truth over sixty deposits, as pool
traffic rises:

=====================  =========  =========================================
Competing withdrawals  Precision  What the match is worth
=====================  =========  =========================================
0 (quiet pool)            100%    the recorded case
1                        56.7%    a coin flip with a story attached
2                        33.3%    wrong twice as often as right
4                         8.3%    noise
10                     refused    no claim made at all
=====================  =========  =========================================

These are measured, and measuring them changed the design: the collapse is
much steeper than it looks like it should be. The intuition says one competitor
halves precision, two leaves a third, and so on --- but a competitor only has
to land *anywhere earlier* than the true withdrawal to win, so precision tracks
the chance that none of them did, and that falls off geometrically. Four
competitors is already noise, not a weak signal.

That is why :data:`MAX_ANONYMITY_SET` is five rather than the twenty a first
guess suggested. See ``tests/validation/test_mixer_correlation_accuracy.py``.

**Never HIGH.** A timing coincidence is circumstantial by nature. It narrows a
hypothesis and cannot confirm one, and the ceiling is what stops it being
quoted as though it had.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "MAX_ANONYMITY_SET",
    "MixerEvent",
    "MixerMatch",
    "correlate_withdrawals",
]

#: Above this many competing withdrawals in the window, no claim is made at all.
#:
#: Not a tuning knob for coverage. Measured precision is 8.3% at four
#: competitors --- not a weak finding but a wrong one with a plausible shape,
#: and it would sit in a report beside genuine matches with nothing to tell
#: them apart six months later.
#:
#: Five, because that is where the measurement put it. A first guess would have
#: allowed far more: it seems as though ``n`` competitors should leave roughly
#: ``1/n`` precision, which would make twenty tolerable. The real curve is
#: geometric, because a competitor wins by landing anywhere earlier than the
#: true withdrawal, not by being chosen at random.
MAX_ANONYMITY_SET = 5


@dataclass(frozen=True, slots=True)
class MixerEvent:
    """A deposit into, or a withdrawal from, a mixer pool."""

    tx: str
    block: int
    address: str
    """The depositor for a deposit; the recipient for a withdrawal."""

    index: int = 0
    """Position within the block, so two events in one block still order."""

    @property
    def order(self) -> tuple[int, int]:
        return (self.block, self.index)


@dataclass(frozen=True, slots=True)
class MixerMatch:
    """A deposit paired with a withdrawal, and how much competition it had."""

    deposit: MixerEvent
    withdrawal: MixerEvent
    anonymity_set: int
    """Withdrawals from the same pool that fell in the same window.

    One means the match was unopposed. This is the number that decides whether
    the pairing means anything, so it is a field rather than a derived detail.
    """

    gap_blocks: int

    @property
    def confidence(self) -> Confidence:
        """Falls with competition, and never reaches HIGH.

        A timing coincidence is circumstantial however clean it looks. The
        recorded thirteen-for-thirteen case would land at MEDIUM here, which is
        correct: it was strong evidence and it was still an inference.
        """
        if self.anonymity_set <= 1:
            return Confidence.MEDIUM
        if self.anonymity_set <= 3:
            return Confidence.LOW
        return Confidence.SPECULATIVE

    def summary(self) -> str:
        if self.anonymity_set <= 1:
            competition = (
                "No other withdrawal from this pool fell in the window, so the "
                "pairing is unopposed"
            )
        else:
            competition = (
                f"{self.anonymity_set} withdrawals fell in the same window, so "
                f"this is one of {self.anonymity_set} equally consistent pairings"
            )
        return (
            f"{self.deposit.address} deposited at block {self.deposit.block}; "
            f"{self.withdrawal.address} withdrew {self.gap_blocks} blocks later. "
            f"{competition}. The mixer's cryptography is not broken here --- this "
            f"is a claim about operator timing, and a depositor who waited, or "
            f"withdrew out of order, would not appear at all."
        )

    def attribution(self, chain: ChainId | None = None) -> Attribution:
        return Attribution(
            label=f"probable withdrawal for {self.deposit.address}",
            category=Category.MIXER,
            confidence=self.confidence,
            method=Method.INFERENCE,
            source="chainscope mixer timing correlation",
            address=self.withdrawal.address,
            chain=chain,
            rationale=self.summary(),
        )


@dataclass
class CorrelationResult:
    """Matches, and everything that did not match."""

    matches: list[MixerMatch] = field(default_factory=list)
    unmatched: list[MixerEvent] = field(default_factory=list)
    """Deposits with no withdrawal in range, or too many.

    Reported rather than dropped. A result listing nine matches from thirteen
    deposits reads as nine matches unless the other four are visible, and "we
    looked and could not say" is the answer that has to survive.
    """

    ambiguous: dict[str, int] = field(default_factory=dict)
    """Deposit tx to the number of competing withdrawals that made it unusable."""

    def summary(self) -> str:
        total = len(self.matches) + len(self.unmatched)
        if not total:
            return "no deposits to correlate"
        clean = sum(1 for m in self.matches if m.anonymity_set <= 1)
        parts = [f"{len(self.matches)} of {total} deposits paired"]
        if clean:
            parts.append(f"{clean} unopposed")
        if self.ambiguous:
            parts.append(f"{len(self.ambiguous)} left unpaired for having too much company")
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": len(self.matches),
            "unmatched": len(self.unmatched),
            "ambiguous": dict(self.ambiguous),
            "matches": [
                {
                    "deposit_tx": m.deposit.tx,
                    "depositor": m.deposit.address,
                    "withdrawal_tx": m.withdrawal.tx,
                    "recipient": m.withdrawal.address,
                    "gap_blocks": m.gap_blocks,
                    "anonymity_set": m.anonymity_set,
                    "confidence": m.confidence.name,
                }
                for m in self.matches
            ],
        }


def correlate_withdrawals(
    deposits: list[MixerEvent],
    withdrawals: list[MixerEvent],
    *,
    window_blocks: int = 100,
    max_anonymity_set: int = MAX_ANONYMITY_SET,
) -> CorrelationResult:
    """Pair each deposit with the withdrawal that most likely belongs to it.

    The window is the operator-behaviour assumption made explicit: somebody who
    deposits and withdraws within a hundred blocks is not using the mixer for
    anonymity so much as for a hop, and that is who this finds. Widening it does
    not find more careful operators --- it finds more competitors per deposit
    and lowers every confidence accordingly, which is the honest response and
    not a failure of the parameter.

    A withdrawal already claimed by an earlier deposit is not offered again.
    Without that, one popular withdrawal is assigned to every deposit near it
    and the result looks like a cluster of matches rather than one contested
    guess repeated.
    """
    if window_blocks <= 0:
        raise ValueError("window_blocks must be positive")

    ordered_deposits = sorted(deposits, key=lambda e: e.order)
    ordered_withdrawals = sorted(withdrawals, key=lambda e: e.order)

    claimed: set[str] = set()
    result = CorrelationResult()

    for deposit in ordered_deposits:
        candidates = [
            w
            for w in ordered_withdrawals
            if w.tx not in claimed
            and w.order > deposit.order
            and w.block - deposit.block <= window_blocks
        ]
        if not candidates:
            result.unmatched.append(deposit)
            continue

        if len(candidates) > max_anonymity_set:
            # Refused, not weakened. At this much competition the nearest
            # withdrawal is barely likelier than any other, and a SPECULATIVE
            # claim recorded here sits beside real ones with nothing to tell
            # them apart six months later.
            result.unmatched.append(deposit)
            result.ambiguous[deposit.tx] = len(candidates)
            continue

        chosen = candidates[0]
        claimed.add(chosen.tx)
        result.matches.append(
            MixerMatch(
                deposit=deposit,
                withdrawal=chosen,
                anonymity_set=len(candidates),
                gap_blocks=chosen.block - deposit.block,
            )
        )

    return result
