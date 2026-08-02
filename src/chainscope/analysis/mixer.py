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
from datetime import datetime, timezone
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context

__all__ = [
    "DEPOSIT_TOPIC",
    "MAX_ANONYMITY_SET",
    "TORNADO_ETH_POOLS",
    "TORNADO_ROUTER",
    "WITHDRAWAL_TOPIC",
    "MixerAnalyzer",
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


# --------------------------------------------------------------------- pools

#: Tornado Cash ETH pools, by denomination. OFAC-sanctioned since August 2022;
#: they are listed here because tracing funds *through* a sanctioned mixer is
#: the ordinary work of an investigation, and an analyst who has to paste
#: addresses from a blog post will paste one wrong eventually.
TORNADO_ETH_POOLS: dict[str, str] = {
    "0.1": "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc",
    "1": "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
    "10": "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
    "100": "0xa160cdab225685da1d56aa342ad8841c3b53f291",
}

#: The Router almost everything goes through since 2021.
#:
#: This is the trap worth writing down. Deposits are made by calling the
#: *Router*, not the pool, so a filter that matches only pool addresses finds
#: nothing at all --- and finds it silently, which reads as "this address never
#: touched Tornado". Field notes record a whole case where every deposit went
#: through the Router.
TORNADO_ROUTER = "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b"

#: ``Withdrawal(address to, bytes32 nullifierHash, address indexed relayer,
#: uint256 fee)``. ``to`` is the first non-indexed word of the data, which is
#: what makes withdrawals enumerable without touching each transaction.
WITHDRAWAL_TOPIC = "0xe9e508bad6d4c3227e881ca19068f099da81b5164dd6d62b2eaf1e8bc6c34931"

#: ``Deposit(bytes32 indexed commitment, uint32 leafIndex, uint256 timestamp)``.
#:
#: The depositor is **not** in this event. It is the transaction sender, so
#: enumerating deposits gives transaction hashes and each one still has to be
#: resolved to find who made it --- which is a real cost and the reason this
#: analyzer caps how many it will do.
DEPOSIT_TOPIC = "0xa945e51eec50ab98c161376f0db4cf2aeba3ec92755fe2fcd388bdbbb80ff196"


def _word(data: str, index: int) -> str:
    """The ``index``-th 32-byte word of a log's data field."""
    body = data[2:] if data.startswith("0x") else data
    start = index * 64
    return body[start : start + 64]


def parse_withdrawal(log: dict[str, Any], chain_hint: str = "") -> MixerEvent | None:
    """Turn a ``Withdrawal`` log into an event, or ``None`` if it is not one.

    ``None`` rather than a guess. A log that does not parse is not a withdrawal
    to nobody --- it is something this function does not understand, and
    inventing an event for it would put an address in a correlation that never
    withdrew anything.
    """
    topics = log.get("topics") or []
    if not topics or str(topics[0]).lower() != WITHDRAWAL_TOPIC:
        return None
    data = str(log.get("data") or "")
    recipient = _word(data, 0)
    if len(recipient) != 64:
        return None
    try:
        block = int(
            str(log.get("blockNumber")), 16 if "x" in str(log.get("blockNumber")) else 10
        )
        index = int(
            str(log.get("logIndex") or "0"), 16 if "x" in str(log.get("logIndex")) else 10
        )
    except (TypeError, ValueError):
        return None
    return MixerEvent(
        tx=str(log.get("transactionHash") or ""),
        block=block,
        # Last twenty bytes of the word. Taking the whole word would produce a
        # 32-byte string that matches no address anywhere downstream.
        address="0x" + recipient[-40:],
        index=index,
    )


class MixerAnalyzer(Analyzer):
    """Correlate deposits and withdrawals for one mixer pool."""

    name = "mixer"
    version = "1.0"
    description = "Pair mixer deposits with withdrawals by timing, with the anonymity set"

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, Capability.LOGS))

    def run(
        self,
        ctx: Context,
        *,
        pool: str = "10",
        from_block: int = 0,
        to_block: int | str = "latest",
        window_blocks: int = 100,
        deposits: str = "",
        **_: Any,
    ) -> Result:
        """Enumerate a pool's withdrawals and pair them with known deposits.

        ``deposits`` is a comma-separated list of deposit transaction hashes.
        They are required rather than discovered, and that is a deliberate
        limit: the ``Deposit`` event does not carry the depositor --- it is the
        transaction sender --- so discovering them means resolving every deposit
        in the range, and a busy pool has tens of thousands. An investigation
        arrives here already knowing which deposits are its own.
        """
        started = datetime.now(timezone.utc)
        address = TORNADO_ETH_POOLS.get(pool, pool).lower()
        warnings: list[str] = []

        if not deposits.strip():
            raise ValueError(
                "mixer correlation needs `deposits`: a comma-separated list of "
                "deposit transaction hashes. The Deposit event does not name the "
                "depositor, so they cannot be discovered from logs alone."
            )

        # The whole reason corroborate exists. An enumerative log query that
        # silently returns a short list produces a *smaller anonymity set*,
        # which inflates every confidence in the result -- the failure mode
        # here is not a missing row but a stronger claim than the data supports.
        found = ctx.router.corroborate(
            ctx.chain,
            Capability.LOGS,
            lambda p: p.get_logs(
                ctx.chain,
                address=address,
                topics=[WITHDRAWAL_TOPIC],
                from_block=from_block,
                to_block=to_block,
            ),
            key=lambda log: (
                str(log.get("transactionHash", "")).lower(),
                str(log.get("logIndex", "")),
            ),
        )
        if not found.corroborated:
            warnings.append(
                f"withdrawal enumeration {found.summary()} A short list here does "
                f"not merely lose rows --- it shrinks the anonymity set, and every "
                f"confidence below is computed from that number."
            )

        withdrawals = [w for w in (parse_withdrawal(log) for log in found.rows) if w]
        unparsed = len(found.rows) - len(withdrawals)
        if unparsed:
            warnings.append(
                f"{unparsed} of {len(found.rows)} logs did not parse as Withdrawal "
                f"events and were dropped rather than guessed at"
            )
        if not withdrawals:
            return self._result(
                ctx,
                warnings=(*warnings, f"no withdrawals found for pool {address} in range"),
                params={"pool": address},
                started=started,
            )

        wanted = {h.strip().lower() for h in deposits.split(",") if h.strip()}
        by_tx = {w.tx.lower(): w for w in withdrawals}
        deposit_events = [
            MixerEvent(tx=h, block=by_tx[h].block, address=h)
            for h in sorted(wanted)
            if h in by_tx
        ]
        missing = wanted - set(by_tx)
        if missing:
            # Deposit blocks have to come from somewhere. Rather than fetch each
            # transaction, unknown hashes are reported as unresolvable -- which
            # is honest, and better than a correlation built on a guessed block.
            warnings.append(
                f"{len(missing)} deposit hashes could not be placed in a block "
                f"from the withdrawal logs alone and were skipped: they need "
                f"`chainscope tx` to resolve first"
            )

        result = correlate_withdrawals(deposit_events, withdrawals, window_blocks=window_blocks)
        findings = [
            Finding(
                title=(
                    f"{m.withdrawal.address} is a probable withdrawal "
                    f"({m.anonymity_set} candidate"
                    f"{'' if m.anonymity_set == 1 else 's'})"
                ),
                severity=Severity.NOTABLE if m.anonymity_set <= 1 else Severity.INFO,
                detail=m.summary(),
                data={
                    "recipient": m.withdrawal.address,
                    "withdrawal_tx": m.withdrawal.tx,
                    "gap_blocks": m.gap_blocks,
                    "anonymity_set": m.anonymity_set,
                    "confidence": m.confidence.name,
                },
            )
            for m in result.matches
        ]
        if result.ambiguous:
            warnings.append(
                f"{len(result.ambiguous)} deposits had more than "
                f"{MAX_ANONYMITY_SET} candidate withdrawals and were left unpaired"
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={"pool": address, "window_blocks": window_blocks},
            started=started,
        )
