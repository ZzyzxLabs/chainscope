"""Cross-chain matching.

Funds enter a service on one chain and leave on another. Nothing on either chain
links the two sides --- no shared address, no reference, no receipt. The only
correspondence is that a deposit at time *T* produces a payout shortly after, of
an amount that matches at the rate then prevailing.

That makes this a search problem over three dimensions:

**Time.** Usually the strongest constraint. A service that settles within an
hour narrows Bitcoin to two or three blocks, which cuts the candidate set by
orders of magnitude before anything else is considered.

**Amount.** Not the spot-equivalent. Services quote with a spread and take a
fee, so the payout is consistently *below* spot --- typically by a stable
percentage. Searching at spot finds nothing; widening the tolerance to
compensate floods the results. Calibrating the discount from confirmed cases and
searching around that is what makes this tractable. See :func:`calibrate`.

**Payer.** Once candidates remain, the paying wallet distinguishes them. A
service hot wallet has tens of thousands of transactions; a one-off address has
two. A payout also lands on a fresh address and returns change to the payer ---
a shape that ordinary transfers do not have.

**This produces hypotheses, never conclusions.** A high-scoring match is a lead
worth verifying by other means, and the analyzer caps its confidence at ``LOW``
accordingly. Two unrelated people can send similar amounts minutes apart.
See ``docs/methods/cross-chain.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..core.attribution import Confidence
from ..core.hypothesis import Hypothesis, ScoreFactor, rank
from ..core.result import Finding, Result, Severity
from ..core.units import Amount
from ..pricing.base import PriceSource, RateError
from .base import Analyzer, Context

__all__ = ["Calibration", "CrossChainMatcher", "PayoutCandidate", "calibrate"]


@dataclass(frozen=True, slots=True)
class PayoutCandidate:
    """One output that could be the far side of a swap."""

    txid: str
    index: int
    amount: Amount
    recipient: str | None
    payer: str | None
    block: int
    block_time: datetime
    delay_seconds: int
    is_change: bool = False
    payer_tx_count: int = -1
    recipient_tx_count: int = -1
    output_count: int = 2


@dataclass(frozen=True, slots=True)
class Calibration:
    """The effective discount a service applies, measured from known swaps."""

    mean_discount: Decimal
    spread: Decimal
    """Difference between the largest and smallest observed discount.

    The number that matters. A spread under about 0.2 percentage points across
    independent swaps is very hard to explain by coincidence, and is stronger
    evidence that the same service handled them than any single match."""

    samples: int

    @property
    def is_consistent(self) -> bool:
        return self.spread < Decimal("0.2")

    def band(self, tolerance: Decimal = Decimal("0.5")) -> tuple[Decimal, Decimal]:
        """Discount range to search, as percentages."""
        return (self.mean_discount - tolerance, self.mean_discount + tolerance)


def calibrate(
    cases: list[tuple[Decimal, str, datetime, Decimal]],
    quote_asset: str,
    prices: PriceSource,
) -> Calibration:
    """Measure a service's effective discount from confirmed swaps.

    ``cases`` are ``(sent amount, sent asset, time, received amount)``.

    Do this before searching for unknown matches. It converts "somewhere below
    spot, we think" into a specific band, and the consistency of the result is
    itself evidence about whether one service handled all the cases.
    """
    discounts: list[Decimal] = []
    for sent, asset, when, received in cases:
        quote = prices.rate(asset, quote_asset, when)
        expected = quote.convert(sent)
        if expected == 0:
            continue
        discounts.append((1 - received / expected) * 100)
    if not discounts:
        raise ValueError("no usable cases --- check the assets and timestamps")
    total = Decimal(0)
    for d in discounts:
        total += d
    return Calibration(
        mean_discount=total / len(discounts),
        spread=max(discounts) - min(discounts),
        samples=len(discounts),
    )


class CrossChainMatcher(Analyzer):
    """Find the far side of a swap on another chain."""

    name = "cross-chain"
    version = "1.0"
    description = "Match a deposit on one chain to its payout on another"

    def __init__(self, prices: PriceSource, scanner: Any = None) -> None:
        self.prices = prices
        self.scanner = scanner
        """Object exposing ``outputs_between(t_lo, t_hi, min_amount, max_amount)``.

        Kept injectable because enumerating candidate outputs differs completely
        between UTXO and account chains, and neither belongs in this file."""

    def run(
        self,
        ctx: Context,
        *,
        amount: str = "",
        asset: str = "",
        at: datetime | None = None,
        target_asset: str = "BTC",
        window: tuple[int, int] = (300, 2700),
        discount_band: tuple[float, float] = (0.0, 6.0),
        calibration: Calibration | None = None,
        top: int = 5,
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not amount or not asset or at is None:
            raise ValueError("cross-chain matching needs `amount`, `asset`, and `at`")
        if at.tzinfo is None:
            raise ValueError("`at` must be timezone-aware")
        if self.scanner is None:
            raise ValueError("no scanner configured for the target chain")

        sent = Decimal(amount)
        warnings: list[str] = []

        try:
            quote = self.prices.rate(asset, target_asset, at)
        except RateError as exc:
            return self._result(
                ctx,
                warnings=(f"cannot match without a rate: {exc}",),
                params=self._params(amount, asset, at, target_asset, window),
                started=started,
            )

        expected = quote.convert(sent)
        if quote.derivation == "via USDT":
            warnings.append(
                "rate was triangulated through USDT and carries two spreads; "
                "consider widening the discount band"
            )

        if calibration is not None:
            lo_pct, hi_pct = calibration.band()
            warnings.append(
                f"searching a calibrated band of {lo_pct:.3f}-{hi_pct:.3f}% "
                f"discount from {calibration.samples} known case(s)"
            )
        else:
            lo_pct, hi_pct = Decimal(str(discount_band[0])), Decimal(str(discount_band[1]))

        hi_amount = expected * (1 - lo_pct / 100)
        lo_amount = expected * (1 - hi_pct / 100)

        t_lo = at + timedelta(seconds=window[0])
        t_hi = at + timedelta(seconds=window[1])

        candidates: list[PayoutCandidate] = self.scanner.outputs_between(
            t_lo=t_lo, t_hi=t_hi, min_amount=lo_amount, max_amount=hi_amount
        )
        payouts = [c for c in candidates if not c.is_change]

        if not payouts:
            return self._result(
                ctx,
                warnings=(
                    *warnings,
                    f"no non-change outputs between {lo_amount:.8f} and "
                    f"{hi_amount:.8f} {target_asset} in the {window[0]}-{window[1]}s "
                    f"window. The swap may have targeted a different asset, or the "
                    f"service's fee may fall outside the searched band.",
                ),
                params=self._params(amount, asset, at, target_asset, window),
                started=started,
            )

        hypotheses = rank([self._score(c, expected, target_asset) for c in payouts])
        best = hypotheses[0]

        findings = [
            Finding(
                title=f"{len(payouts)} candidate payout(s); best scores {best.score:g}",
                severity=Severity.NOTABLE,
                detail=(
                    f"{sent} {asset} at {quote.rate} {target_asset}/{asset} implies "
                    f"{expected:.8f} {target_asset} before fees. "
                    f"{best.claim}"
                ),
                data={
                    "expected": str(expected),
                    "rate": str(quote.rate),
                    "rate_derivation": quote.derivation,
                    "searched": [str(lo_amount), str(hi_amount)],
                    "candidates": len(payouts),
                },
                evidence=ctx.evidence(),
            )
        ]

        if best.is_contested:
            warnings.append(
                "the top two candidates score within 1.0 of each other; this "
                "ranking is not decisive and should not be reported as a match"
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            hypotheses=tuple(hypotheses[:top]),
            warnings=tuple(warnings),
            params=self._params(amount, asset, at, target_asset, window),
            started=started,
        )

    # ---------------------------------------------------------------- scoring

    @staticmethod
    def _score(c: PayoutCandidate, expected: Decimal, asset: str) -> Hypothesis:
        discount = (1 - c.amount.decimal / expected) * 100 if expected else Decimal(0)
        factors = [
            ScoreFactor(
                "payer_is_service_scale",
                weight=5.0,
                value=c.payer_tx_count >= 10_000,
                note=f"payer has {c.payer_tx_count:,} transactions"
                if c.payer_tx_count >= 0
                else "payer activity unknown",
            ),
            ScoreFactor(
                "payer_is_one_off",
                weight=-3.0,
                value=0 <= c.payer_tx_count <= 3,
                note="a wallet with almost no history is not a service",
            ),
            ScoreFactor(
                "recipient_is_fresh",
                weight=3.0,
                value=0 <= c.recipient_tx_count <= 2,
                note="payouts land on newly created addresses",
            ),
            ScoreFactor(
                "payout_plus_change_shape",
                weight=1.5,
                value=c.output_count == 2,
                note="two outputs: one payment, one change",
            ),
            ScoreFactor(
                "discount_is_plausible",
                weight=2.0,
                value=Decimal(0) <= discount <= Decimal(5),
                note=f"{discount:.3f}% below spot",
            ),
        ]
        return Hypothesis(
            claim=(
                f"{c.amount} to {c.recipient} in {c.txid[:16]}… "
                f"(+{c.delay_seconds}s, {discount:.3f}% below spot)"
            ),
            factors=tuple(factors),
            # Capped low on purpose: this is circumstantial correspondence, not
            # an observed link. Two strangers can transact similar amounts
            # minutes apart.
            confidence=Confidence.LOW,
            data={
                "txid": c.txid,
                "vout": c.index,
                "amount_raw": c.amount.raw,
                "amount": str(c.amount),
                "recipient": c.recipient,
                "payer": c.payer,
                "delay_seconds": c.delay_seconds,
                "discount_percent": str(discount),
                "asset": asset,
            },
        )

    @staticmethod
    def _params(
        amount: str, asset: str, at: datetime, target: str, window: tuple[int, int]
    ) -> dict[str, Any]:
        return {
            "amount": amount,
            "asset": asset,
            "at": at.isoformat(),
            "target_asset": target,
            "window_seconds": list(window),
        }
