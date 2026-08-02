"""Peel chains.

A common laundering shape on UTXO chains: a large output spends, a small amount
is peeled off to some destination, and the remainder --- the change --- carries
on to the next transaction. Repeated, it produces a long chain where the main
line holds most of the value and each step sheds a little.

Following it requires deciding, at every hop, which output is the payment and
which is the change. Get that backwards once and you follow the payment into a
dead end while the actual funds walk away, and nothing about the output tells
you which mistake you made.

So the decision is made by weighted heuristics whose votes are recorded, and the
analyzer stops rather than guessing when they disagree.

**Change-detection heuristics, strongest first:**

1. *Address reuse* (+5). An output paying back into the input set is change.
   Close to conclusive when present, and absent from most modern wallets.
2. *Round numbers* (-2). Humans pay round amounts. An output of exactly 2.0 BTC
   alongside one of 8.33478 BTC identifies which is which.
3. *Script type match* (+2). Wallets generate change matching their own script
   type; a payment to a different type is likely an external party.
4. *Largest output* (+2). Weak on its own --- a peel chain's change is larger by
   construction, but so is a large payment with small change.
5. *Fresh recipient* (-1). The weakest, and it reads backwards at first: every
   HD wallet derives change to a fresh address, so freshness is a property of
   change by construction, and payees are frequently fresh too. It was weighted
   -3 until ``tests/validation`` showed that choosing the *payee* whenever
   change was the smaller output cost more than the signal was worth.

**When this fails:** see ``docs/methods/change-detection.md``. CoinJoin defeats
it outright, consolidation transactions have no change at all, and a wallet that
pays round amounts to itself inverts heuristic 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..core.attribution import Confidence
from ..core.hypothesis import Hypothesis, ScoreFactor
from ..core.result import Finding, Result, Severity
from ..core.units import Amount
from .base import Analyzer, Context

__all__ = ["ChangeDecision", "PeelChainAnalyzer", "PeelStep", "detect_change"]


@dataclass(frozen=True, slots=True)
class Output:
    """One transaction output, as the analyzer needs it."""

    index: int
    address: str | None
    amount: Amount
    script_type: str = ""
    recipient_tx_count: int = -1


@dataclass(frozen=True, slots=True)
class ChangeDecision:
    """Which output was judged to be change, and on what basis."""

    index: int | None
    hypothesis: Hypothesis

    @property
    def confident(self) -> bool:
        """Whether the decision is clear enough to keep following the chain.

        A contested decision means the next hop is a guess, and a peel chain
        followed through a guess is worse than one that stops: it looks equally
        authoritative and is wrong from that point on.
        """
        return self.index is not None and not self.hypothesis.is_contested


def detect_change(
    outputs: list[Output],
    input_addresses: set[str],
    input_script_types: set[str],
) -> ChangeDecision:
    """Decide which output is change."""
    if not outputs:
        return ChangeDecision(
            None,
            Hypothesis(claim="no outputs", confidence=Confidence.SPECULATIVE),
        )
    if len(outputs) == 1:
        return ChangeDecision(
            0,
            Hypothesis(
                claim="single output; nothing to distinguish",
                factors=(ScoreFactor("sole_output", 1.0, True),),
                confidence=Confidence.MEDIUM,
            ),
        )

    # Only a *strictly* largest output counts. With two equal outputs, max()
    # returns whichever came first, which would hand one of them a decisive
    # two-point edge invented from nothing -- exactly the arbitrary tiebreak
    # this analyzer exists to refuse to make.
    largest_raw = max(o.amount.raw for o in outputs)
    unique_largest = (
        next(o.index for o in outputs if o.amount.raw == largest_raw)
        if sum(1 for o in outputs if o.amount.raw == largest_raw) == 1
        else None
    )

    scored: list[tuple[Output, list[ScoreFactor]]] = []
    for out in outputs:
        value = out.amount.decimal
        reused = bool(out.address and out.address in input_addresses)
        # "Round" at 3dp: 0.1, 2.5, 10 read as human-chosen; 8.33478 does not.
        looks_round = value == value.quantize(Decimal("0.001")) and value >= Decimal("0.001")
        factors = [
            ScoreFactor(
                "pays_back_into_input_set",
                5.0,
                reused,
                note="output address appears among the inputs",
            ),
            ScoreFactor(
                "recipient_is_fresh",
                # Weak on purpose, and it used to be -3.0 --- the second
                # strongest weight in this table, which was backwards. Every HD
                # wallet derives change to a fresh address, so freshness is a
                # property of change *by construction*. Payees are frequently
                # fresh too, so it barely separates them in either direction.
                #
                # The literature's version is sharper: Meiklejohn et al.'s
                # one-time change address is one that receives once and never
                # appears again. That requires knowing the future, which nothing
                # has at this point in a trace.
                #
                # Measured over the scenarios in tests/validation: at -3.0 this
                # scored 5/8 and chose the *payee* whenever change was the
                # smaller output; at -1.0 it scores 6/8, and nothing weaker than
                # -1.5 changes the outcome at all. That is the honest summary of
                # what this signal is worth.
                -1.0,
                0 <= out.recipient_tx_count <= 1,
                note="a fresh address is weak evidence of a payee; change is usually fresh too",
            ),
            ScoreFactor(
                "round_number",
                -2.0,
                looks_round,
                note=f"{value} looks like a human-chosen amount",
            ),
            ScoreFactor(
                "script_type_matches_inputs",
                2.0,
                bool(out.script_type and out.script_type in input_script_types),
                note="wallets generate change of their own script type",
            ),
            ScoreFactor(
                "largest_output",
                2.0,
                unique_largest is not None and out.index == unique_largest,
                note="peel-chain change carries most of the value",
            ),
        ]
        scored.append((out, factors))

    ranked = sorted(scored, key=lambda pair: -sum(f.contribution for f in pair[1]))
    best, best_factors = ranked[0]
    alternatives = tuple(
        Hypothesis(
            claim=f"output {o.index} ({o.amount}) is change",
            factors=tuple(fs),
            confidence=Confidence.MEDIUM,
        )
        for o, fs in ranked[1:]
    )
    hypothesis = Hypothesis(
        claim=f"output {best.index} ({best.amount}) is change",
        factors=tuple(best_factors),
        confidence=Confidence.MEDIUM,
        alternatives=alternatives,
        data={"index": best.index, "address": best.address},
    )
    return ChangeDecision(best.index, hypothesis)


@dataclass
class PeelStep:
    """One hop along a peel chain."""

    depth: int
    txid: str
    timestamp: datetime | None
    total_in: Amount
    peeled: list[Output] = field(default_factory=list)
    change: Output | None = None
    decision: ChangeDecision | None = None
    stopped_because: str = ""


class PeelChainAnalyzer(Analyzer):
    """Follow the change output of a transaction chain, hop by hop."""

    name = "peel-chain"
    version = "1.0"
    description = "Follow a UTXO peel chain, identifying payments shed at each hop"

    def __init__(self, walker: Any = None) -> None:
        self.walker = walker
        """Object exposing ``transaction(txid)`` and ``spent_by(address, after_txid)``.

        Injected because UTXO traversal differs per chain and per data source."""

    def run(
        self,
        ctx: Context,
        *,
        start: str = "",
        max_depth: int = 10,
        min_peel: str = "0",
        stop_when_uncertain: bool = True,
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not start:
            raise ValueError("peel-chain analysis needs a `start` txid")
        if self.walker is None:
            raise ValueError("no walker configured for this chain")

        threshold = Decimal(min_peel)
        steps: list[PeelStep] = []
        warnings: list[str] = []
        hypotheses: list[Hypothesis] = []
        seen: set[str] = set()
        txid = start

        for depth in range(max_depth):
            if txid in seen:
                warnings.append(f"cycle detected at {txid}; stopping")
                break
            seen.add(txid)

            tx = self.walker.transaction(txid)
            if tx is None:
                warnings.append(f"could not retrieve {txid}; chain truncated here")
                break

            decision = detect_change(
                tx.outputs, set(tx.input_addresses), set(tx.input_script_types)
            )
            hypotheses.append(decision.hypothesis)

            change = (
                tx.outputs[decision.index]
                if decision.index is not None and decision.index < len(tx.outputs)
                else None
            )
            peeled = [
                o
                for o in tx.outputs
                if o.index != decision.index and o.amount.decimal >= threshold
            ]
            step = PeelStep(
                depth=depth,
                txid=txid,
                timestamp=tx.timestamp,
                total_in=tx.total_in,
                peeled=peeled,
                change=change,
                decision=decision,
            )
            steps.append(step)

            if not decision.confident:
                step.stopped_because = "change output could not be determined"
                if stop_when_uncertain:
                    warnings.append(
                        f"stopped at depth {depth}: the change decision at {txid} is "
                        f"contested. Following a peel chain through a guess produces "
                        f"a trail that looks authoritative and is wrong from here on."
                    )
                    break

            if change is None or not change.address:
                step.stopped_because = "no change output; chain ends"
                break

            nxt = self.walker.spent_by(change.address, txid)
            if nxt is None:
                step.stopped_because = "change output is unspent"
                break
            txid = nxt

        if len(steps) == max_depth:
            warnings.append(
                f"reached max_depth={max_depth}; the chain may continue beyond this"
            )

        findings = self._summarise(ctx, steps, threshold)
        return self._result(
            ctx,
            findings=tuple(findings),
            hypotheses=tuple(hypotheses),
            warnings=tuple(warnings),
            params={
                "start": start,
                "max_depth": max_depth,
                "min_peel": min_peel,
                "stop_when_uncertain": stop_when_uncertain,
            },
            started=started,
        )

    @staticmethod
    def _summarise(ctx: Context, steps: list[PeelStep], threshold: Decimal) -> list[Finding]:
        if not steps:
            return []
        destinations: dict[str, int] = {}
        peeled_total = 0
        symbol = steps[0].total_in.symbol
        decimals = steps[0].total_in.decimals
        for s in steps:
            for out in s.peeled:
                peeled_total += out.amount.raw
                if out.address:
                    destinations[out.address] = (
                        destinations.get(out.address, 0) + out.amount.raw
                    )

        last = steps[-1]
        findings = [
            Finding(
                title=f"peel chain of {len(steps)} hop(s), {len(destinations)} destination(s)",
                severity=Severity.NOTABLE,
                detail=(
                    f"{Amount(peeled_total, decimals, symbol)} was shed across "
                    f"{len(steps)} hops. "
                    + (
                        f"The chain ends because {last.stopped_because}."
                        if last.stopped_because
                        else "The chain was still running when the walk stopped."
                    )
                ),
                data={
                    "hops": len(steps),
                    "peeled_raw": peeled_total,
                    "destinations": [
                        {"address": a, "raw": v, "amount": str(Amount(v, decimals, symbol))}
                        for a, v in sorted(destinations.items(), key=lambda kv: -kv[1])
                    ],
                    "final_txid": last.txid,
                    "final_change": str(last.change.amount) if last.change else None,
                    "stopped_because": last.stopped_because,
                },
                evidence=ctx.evidence(),
            )
        ]
        contested = [s for s in steps if s.decision and not s.decision.confident]
        if contested:
            findings.append(
                Finding(
                    title=f"{len(contested)} hop(s) had an ambiguous change output",
                    severity=Severity.IMPORTANT,
                    detail=(
                        "At these hops the heuristics did not clearly separate "
                        "payment from change. Everything downstream of the first "
                        "such hop rests on a coin flip."
                    ),
                    data={"depths": [s.depth for s in contested]},
                    evidence=ctx.evidence(),
                )
            )
        return findings
