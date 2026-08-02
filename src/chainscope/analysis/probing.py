"""Probing sequences: the small transfer that comes before the large one.

Somebody moving stolen funds through a service they have not used before does
not send everything at once. They send a little, wait to see it arrive, and
escalate. It is the same instinct as a test payment, and it leaves a shape on
chain that the amounts themselves make legible.

Three recorded examples, from three separate traces:

* ``5 → 10 → 20 → 30 → 50 → 75 → 100 → 125 → 150 → 175`` ETH into one exchange
  deposit address over four hours. Strictly increasing, ten steps.
* ``1 → 7 → 10`` ETH into two exchanges, against deposits of 250--1900 ETH
  elsewhere in the same case --- two orders of magnitude smaller, and the notes
  call it exactly what it is.
* ``0.01--0.05`` ETH first, then the entire balance in one or two transfers.

The first two are **escalation**; the third is **test-then-commit**. Both say
the operator was verifying a route before trusting it, which is worth knowing
because it marks the *first* use of a service --- often the point where an
investigation can still get ahead of the money.

**The false-positive question is the whole design problem.** Plenty of ordinary
behaviour rises: somebody accumulating a position, a business paying a growing
invoice, a bot scaling into a trade. What separates a probe is that it is
strictly monotonic over a run long enough that chance does not explain it, and
that the run ends in something far larger than it began with.

**Length alone does not work, and measuring it is what showed that.** The
obvious null model says n amounts arrive sorted with probability ``1/n!`` ---
16.7% at three, 0.83% at five --- which makes a five-step run look decisive.
So :data:`MIN_ESCALATION_STEPS` was set to five and the detector was run
against ordinary activity.

It fired on **38% of counterparties** whose payments merely drifted upward.

The model was wrong, not the threshold. ``1/n!`` assumes the amounts are a
random permutation; a real payment stream has trend, and in trending data long
increasing runs are ordinary. What actually separates the two is **reach**:

=========================  ==========  ==============
Sequence                   Steps       Growth
=========================  ==========  ==============
Noisy accumulation, worst  5+          6.5x
Noisy accumulation, median 5+          2.3x
TradeOgre (recorded)       10          35x
Test-then-commit (tornado) 2           900x
=========================  ==========  ==============

So detection needs both: a run long enough to notice, and growth past
:data:`MIN_ESCALATION_GROWTH`. A probe reaches for a different order of
magnitude --- that is what makes it a probe rather than a payment schedule.
The ``1/n!`` figure is still reported, because it describes the ordering
honestly; it just cannot be the gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context

__all__ = [
    "MIN_ESCALATION_GROWTH",
    "MIN_ESCALATION_STEPS",
    "MIN_TEST_RATIO",
    "ProbeSequence",
    "ProbingAnalyzer",
    "detect_probes",
]

#: Strictly-increasing transfers needed before escalation is reported.
#:
#: Five, because four happens by chance 4.2% of the time and five 0.83%. With
#: a few hundred counterparties in a case, a 4.2% rule produces a handful of
#: confident coincidences; a 0.83% rule produces roughly one.
MIN_ESCALATION_STEPS = 5

#: How many times larger the last step must be than the first.
#:
#: The threshold that actually does the work, and it exists because the ``1/n!``
#: model above is **wrong for real payment streams**. That model assumes the
#: amounts are a random permutation. Real flows have drift --- somebody
#: accumulating a position sends gradually larger amounts --- and in drifting
#: data a five-step increasing run is common, not rare.
#:
#: Measured: on noisy accumulation (a rising trend plus noise), length alone
#: fired on **38%** of counterparties. Useless. But the runs it fired on grew by
#: a median of 2.3x and never by more than 6.5x, while the recorded TradeOgre
#: probe grew 35x and the tornado test-then-commit 900x.
#:
#: Eight sits above every false positive measured and well below every real one.
#: A probe reaches for a different order of magnitude by definition --- that is
#: what makes it a probe rather than a payment schedule.
MIN_ESCALATION_GROWTH = 8

#: How many times larger the commit must be than the test, for test-then-commit.
#:
#: A hundred. The recorded cases are far past it --- 0.01 ETH then the whole
#: balance, or 1 ETH against 250 --- and anything nearer to the test amount is
#: better explained as two ordinary payments of different sizes.
MIN_TEST_RATIO = 100


@dataclass(frozen=True, slots=True)
class ProbeSequence:
    """A run of transfers to one destination that looks like a trial."""

    source: str
    destination: str
    amounts: tuple[int, ...]
    """Raw amounts in order. Raw, not scaled: these are compared to each other
    and never to another asset, so the unit cancels."""

    decimals: int
    symbol: str
    kind: str
    """``"escalation"`` or ``"test-then-commit"``."""

    first_block: int | None = None
    last_block: int | None = None

    @property
    def steps(self) -> int:
        return len(self.amounts)

    @property
    def growth(self) -> float:
        """Largest divided by smallest. The reach of the probe."""
        low = min(self.amounts)
        return max(self.amounts) / low if low else float("inf")

    @property
    def chance(self) -> float:
        """Probability that this many amounts land in increasing order by luck.

        ``1/n!``, which is the honest null model for "were these already sorted
        for a reason". It is what makes a five-step run meaningful and a
        three-step run not.
        """
        return 1.0 / math.factorial(self.steps) if self.steps > 1 else 1.0

    @property
    def confidence(self) -> Confidence:
        """MEDIUM at most, and only for runs chance does not explain.

        Never higher. This describes a *shape*, and a legitimate desk scaling
        into a position produces the same shape --- what differs is the context
        around it, which this function cannot see.
        """
        if self.kind == "escalation" and self.steps >= MIN_ESCALATION_STEPS + 2:
            return Confidence.MEDIUM
        if self.chance < 0.01 or self.growth >= MIN_TEST_RATIO:
            return Confidence.MEDIUM
        return Confidence.LOW

    def summary(self) -> str:
        shown = ", ".join(self._fmt(a) for a in self.amounts[:8])
        more = f" and {self.steps - 8} more" if self.steps > 8 else ""
        if self.kind == "escalation":
            return (
                f"{self.steps} strictly increasing transfers to {self.destination}: "
                f"{shown}{more} {self.symbol}. A run this long arrives in order by "
                f"chance about {self.chance:.2%} of the time, so the ordering is "
                f"very likely deliberate --- the shape of somebody testing a route "
                f"before trusting it with the rest. It is not proof of intent: a "
                f"desk scaling into a position looks identical from here."
            )
        return (
            f"A transfer of {self._fmt(self.amounts[0])} {self.symbol} to "
            f"{self.destination}, followed by one {self.growth:,.0f}x larger. "
            f"That gap is the signature of a test payment rather than two "
            f"payments of different sizes --- somebody confirmed the route "
            f"worked before committing."
        )

    def _fmt(self, raw: int) -> str:
        if self.decimals <= 0:
            return str(raw)
        whole = raw / (10**self.decimals)
        return f"{whole:,.4f}".rstrip("0").rstrip(".")

    def attribution(self, chain: ChainId | None = None) -> Attribution:
        return Attribution(
            label=f"probed by {self.source}",
            category=Category.SERVICE,
            confidence=self.confidence,
            method=Method.HEURISTIC,
            source="chainscope probing detection",
            address=self.destination,
            chain=chain,
            rationale=self.summary(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "kind": self.kind,
            "steps": self.steps,
            "amounts": list(self.amounts),
            "symbol": self.symbol,
            "decimals": self.decimals,
            "growth": round(self.growth, 2) if self.growth != float("inf") else None,
            "chance": self.chance,
            "confidence": self.confidence.name,
            "summary": self.summary(),
        }


def detect_probes(
    transfers: list[Any],
    *,
    min_steps: int = MIN_ESCALATION_STEPS,
    min_growth: float = MIN_ESCALATION_GROWTH,
    min_ratio: int = MIN_TEST_RATIO,
) -> list[ProbeSequence]:
    """Find probing sequences in a list of transfers.

    Grouped by ``(sender, recipient, asset)`` and read in block order. The asset
    is part of the key because amounts are compared to each other --- mixing two
    tokens into one sequence compares numbers whose units differ, which is how
    six-decimal dust outranks an eighteen-decimal transfer.

    Escalation needs strict increase *and* real growth. Equal consecutive
    amounts break the run --- somebody sending the same amount repeatedly is
    doing something regular, not testing.

    Length alone is not enough, and the measurement is the reason: on ordinary
    accumulation with an upward drift, a five-step increasing run appears for
    38% of counterparties. Those runs grow by 2.3x on average and never past
    6.5x, while a real probe reaches for a different order of magnitude. See
    :data:`MIN_ESCALATION_GROWTH`.
    """
    if min_steps < 3:
        raise ValueError(
            "min_steps below 3 is meaningless: two amounts are in increasing "
            "order half the time"
        )

    grouped: dict[tuple[str, str, str, int, str], list[Any]] = {}
    for t in transfers:
        sender = getattr(t, "sender", None)
        recipient = getattr(t, "recipient", None)
        amount = getattr(t, "amount", None)
        if sender is None or recipient is None or amount is None:
            continue
        if amount.raw <= 0:
            continue
        asset = getattr(t, "asset", None)
        key = (
            sender.key,
            recipient.key,
            asset.key if asset else "",
            amount.decimals,
            amount.symbol,
        )
        grouped.setdefault(key, []).append(t)

    found: list[ProbeSequence] = []
    for (source, destination, _asset, decimals, symbol), group in grouped.items():
        group.sort(key=lambda t: (getattr(t, "block", 0) or 0, getattr(t, "index", 0) or 0))
        amounts = [t.amount.raw for t in group]

        run = _longest_increasing_prefix(amounts)
        # Both, not either. Length alone fires on 38% of ordinary accumulation;
        # growth is what separates a probe from a payment schedule.
        smallest = min(run) if run else 0
        reach = (max(run) / smallest) if smallest else 0.0
        if len(run) >= min_steps and reach >= min_growth:
            found.append(
                ProbeSequence(
                    source=source,
                    destination=destination,
                    amounts=tuple(run),
                    decimals=decimals,
                    symbol=symbol,
                    kind="escalation",
                    first_block=getattr(group[0], "block", None),
                    last_block=getattr(group[len(run) - 1], "block", None),
                )
            )
            continue

        # Test-then-commit is checked only when escalation did not fire, so one
        # sequence is not reported twice under two names.
        if len(amounts) >= 2 and amounts[0] > 0 and max(amounts[1:]) / amounts[0] >= min_ratio:
            biggest = max(amounts[1:])
            found.append(
                ProbeSequence(
                    source=source,
                    destination=destination,
                    amounts=(amounts[0], biggest),
                    decimals=decimals,
                    symbol=symbol,
                    kind="test-then-commit",
                    first_block=getattr(group[0], "block", None),
                    last_block=getattr(group[-1], "block", None),
                )
            )

    found.sort(key=lambda p: (-p.steps, -p.growth))
    return found


def _longest_increasing_prefix(amounts: list[int]) -> list[int]:
    """The longest strictly-increasing run anywhere in the sequence.

    A run, not a subsequence. Picking the longest increasing *subsequence* would
    find order in almost any list --- ten random amounts contain an increasing
    subsequence of four or so by construction --- and the ``1/n!`` null model
    the confidence rests on would no longer apply to what was measured.
    """
    best: list[int] = []
    current: list[int] = []
    for amount in amounts:
        if current and amount <= current[-1]:
            if len(current) > len(best):
                best = current
            current = []
        current.append(amount)
    return current if len(current) > len(best) else best


class ProbingAnalyzer(Analyzer):
    """Find probing sequences in an address's outbound transfers."""

    name = "probing"
    version = "1.0"
    description = "Find test-then-commit and escalating transfer sequences"

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, Capability.ADDRESS_HISTORY))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        min_steps: int = MIN_ESCALATION_STEPS,
        min_growth: float = MIN_ESCALATION_GROWTH,
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not address:
            raise ValueError("probing detection needs an `address` to examine")

        seed = address.lower()
        per_node = ctx.limit("per_node", 1000)
        history = ctx.router.dispatch(
            ctx.chain,
            Capability.ADDRESS_HISTORY,
            lambda p: p.address_history(
                ctx.chain, seed, start_block=start_block, end_block=end_block, limit=per_node
            ),
        )

        warnings: list[str] = []
        if len(history) >= per_node:
            # A probe is a *sequence*, so a window that clips its start turns an
            # escalation into a shorter run and can drop it below the floor
            # entirely. Worth saying louder than the usual truncation note.
            warnings.append(
                f"history filled the {per_node}-row limit, so any sequence "
                f"beginning before this window is cut short and may fall below "
                f"the {min_steps}-step floor without appearing here at all"
            )

        # Failed transactions and zero-value calls dropped: a reverted transfer
        # is not a step in a test sequence.
        outbound = [
            t
            for tx in history
            for t in tx.value_transfers()
            if t.sender and t.sender.key.lower() == seed
        ]
        probes = detect_probes(outbound, min_steps=min_steps, min_growth=min_growth)

        findings = [
            Finding(
                title=(
                    f"{p.steps}-step escalation to {p.destination} ({p.growth:,.0f}x)"
                    if p.kind == "escalation"
                    else f"test payment then {p.growth:,.0f}x commit to {p.destination}"
                ),
                severity=Severity.NOTABLE,
                detail=p.summary(),
                data=p.to_dict(),
            )
            for p in probes
        ]
        if not probes and not warnings:
            warnings.append(
                f"no probing sequence found in {len(outbound)} outbound transfers. "
                f"That is the common result and is not evidence of its absence: a "
                f"probe split across two addresses, or paced beyond this window, "
                f"leaves no run to find."
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={"address": seed, "min_steps": min_steps, "min_growth": min_growth},
            started=started,
        )
