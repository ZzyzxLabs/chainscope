"""Cross-chain matching.

Two things this must never do: present a coincidence as a match, and stay quiet
when the top two candidates are effectively tied.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from chainscope.analysis.base import Context
from chainscope.analysis.xchain import (
    Calibration,
    CrossChainMatcher,
    PayoutCandidate,
    calibrate,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import BITCOIN
from chainscope.core.units import Amount
from chainscope.pricing.base import PriceSource, Quote, RateError
from chainscope.providers.router import Router

AT = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class FixedRate(PriceSource):
    name = "fixed"

    def __init__(self, rate="0.04", derivation="direct", fail=False):
        self._rate = Decimal(rate)
        self._derivation = derivation
        self._fail = fail

    def rate(self, base, quote, at):
        if self._fail:
            raise RateError("no rate")
        return Quote(base, quote, self._rate, at, self.name, self._derivation)


class FakeScanner:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    def outputs_between(self, *, t_lo, t_hi, min_amount, max_amount):
        self.calls.append((t_lo, t_hi, min_amount, max_amount))
        return [c for c in self.candidates if min_amount <= c.amount.decimal <= max_amount]


def payout(btc, *, delay=900, payer_txs=30_000, recip_txs=2, change=False, txid=None):
    return PayoutCandidate(
        txid=txid or f"{'a' * 63}{len(btc)}",
        index=0,
        amount=Amount.parse(btc, 8, "BTC"),
        recipient="bc1qrecipient",
        payer="bc1qpayer",
        block=800_000,
        block_time=AT + timedelta(seconds=delay),
        delay_seconds=delay,
        is_change=change,
        payer_tx_count=payer_txs,
        recipient_tx_count=recip_txs,
    )


def ctx() -> Context:
    return Context(chain=BITCOIN, router=Router())


def run(candidates, *, rate="0.04", amount="250", **kw):
    m = CrossChainMatcher(FixedRate(rate), FakeScanner(candidates))
    return m.run(ctx(), amount=amount, asset="ETH", at=AT, **kw)


class TestSearchWindow:
    def test_window_and_band_reach_the_scanner(self):
        scanner = FakeScanner([])
        CrossChainMatcher(FixedRate("0.04"), scanner).run(
            ctx(),
            amount="250",
            asset="ETH",
            at=AT,
            window=(300, 2700),
            discount_band=(0.0, 6.0),
        )
        t_lo, t_hi, lo, hi = scanner.calls[0]
        assert (t_lo - AT).total_seconds() == 300
        assert (t_hi - AT).total_seconds() == 2700
        # 250 ETH * 0.04 = 10 BTC, minus 0-6%.
        assert hi == Decimal(10)
        assert lo == Decimal("9.4")

    def test_change_outputs_are_excluded(self):
        res = run([payout("9.7", change=True)])
        assert res.is_empty
        assert any("no non-change outputs" in w for w in res.warnings)

    def test_empty_result_suggests_why(self):
        res = run([])
        (w,) = [w for w in res.warnings if "no non-change" in w]
        assert "different asset" in w and "fee" in w


class TestScoring:
    def test_service_scale_payer_outranks_a_one_off(self):
        res = run(
            [
                payout("9.7", payer_txs=2, txid="b" * 64),
                payout("9.6", payer_txs=45_000, txid="c" * 64),
            ]
        )
        assert res.top.data["txid"] == "c" * 64

    def test_fresh_recipient_contributes(self):
        res = run([payout("9.7", recip_txs=1)])
        names = {f.name for f in res.top.factors if f.contribution > 0}
        assert "recipient_is_fresh" in names

    def test_factors_are_individually_visible(self):
        """A reviewer who disagrees must be able to see which factor to argue with."""
        res = run([payout("9.7")])
        explanation = res.top.explain()
        assert "payer_is_service_scale" in explanation
        assert "discount_is_plausible" in explanation

    def test_confidence_never_exceeds_low(self):
        """Circumstantial correspondence is not an observed link."""
        res = run([payout("9.7")])
        assert res.top.confidence is Confidence.LOW

    def test_alternatives_are_carried(self):
        res = run(
            [
                payout("9.7", payer_txs=45_000, txid="d" * 64),
                payout("9.6", payer_txs=2, txid="e" * 64),
            ]
        )
        assert res.top.alternatives


class TestContestedResults:
    def test_near_tie_is_flagged(self):
        """Reporting a coin flip as a finding is the failure mode here."""
        res = run(
            [
                payout("9.70", txid="f" * 64),
                payout("9.71", txid="0" * 64),
            ]
        )
        assert res.top.is_contested
        assert any("not decisive" in w for w in res.warnings)

    def test_clear_winner_is_not_flagged(self):
        res = run(
            [
                payout("9.7", payer_txs=45_000, recip_txs=1, txid="1" * 64),
                payout("9.6", payer_txs=2, recip_txs=500, txid="2" * 64),
            ]
        )
        assert not res.top.is_contested
        assert not any("not decisive" in w for w in res.warnings)


class TestRateHandling:
    def test_missing_rate_fails_loudly_rather_than_guessing(self):
        m = CrossChainMatcher(FixedRate(fail=True), FakeScanner([payout("9.7")]))
        res = m.run(ctx(), amount="250", asset="ETH", at=AT)
        assert res.is_empty
        assert any("cannot match without a rate" in w for w in res.warnings)

    def test_triangulated_rate_is_disclosed(self):
        m = CrossChainMatcher(
            FixedRate("0.04", derivation="via USDT"), FakeScanner([payout("9.7")])
        )
        res = m.run(ctx(), amount="250", asset="ETH", at=AT)
        assert any("two spreads" in w for w in res.warnings)

    def test_naive_timestamps_are_rejected(self):
        m = CrossChainMatcher(FixedRate(), FakeScanner([]))
        with pytest.raises(ValueError, match="timezone-aware"):
            m.run(ctx(), amount="1", asset="ETH", at=datetime(2026, 1, 1))


class TestCalibration:
    def test_consistent_discounts_are_recognised(self):
        """Independent swaps settling within 0.2pp is very hard to explain
        by coincidence -- stronger evidence than any single match."""
        prices = FixedRate("0.04")
        cal = calibrate(
            [
                (Decimal(250), "ETH", AT, Decimal("9.7050")),  # 2.95%
                (Decimal(1000), "ETH", AT, Decimal("38.8200")),  # 2.95%
                (Decimal(500), "ETH", AT, Decimal("19.4200")),  # 2.90%
            ],
            "BTC",
            prices,
        )
        assert cal.samples == 3
        assert cal.is_consistent
        assert Decimal("2.9") < cal.mean_discount < Decimal("3.0")

    def test_inconsistent_discounts_are_flagged(self):
        cal = calibrate(
            [
                (Decimal(250), "ETH", AT, Decimal("9.9")),  # 1%
                (Decimal(250), "ETH", AT, Decimal("9.0")),  # 10%
            ],
            "BTC",
            FixedRate("0.04"),
        )
        assert not cal.is_consistent

    def test_empty_calibration_raises(self):
        with pytest.raises(ValueError, match="no usable cases"):
            calibrate([], "BTC", FixedRate())

    def test_calibrated_band_narrows_the_search(self):
        cal = Calibration(mean_discount=Decimal("2.95"), spread=Decimal("0.05"), samples=3)
        scanner = FakeScanner([])
        CrossChainMatcher(FixedRate("0.04"), scanner).run(
            ctx(), amount="250", asset="ETH", at=AT, calibration=cal
        )
        _, _, lo, hi = scanner.calls[0]
        # 10 BTC less 2.45-3.45%, far tighter than a blind 0-6% sweep.
        assert Decimal("9.65") < lo < Decimal("9.76")
        assert Decimal("9.65") < hi < Decimal("9.76")


class TestReproducibility:
    def test_params_capture_the_run(self):
        res = run([payout("9.7")])
        assert res.params["amount"] == "250"
        assert res.params["asset"] == "ETH"
        assert res.params["window_seconds"] == [300, 2700]

    def test_missing_arguments_are_rejected(self):
        m = CrossChainMatcher(FixedRate(), FakeScanner([]))
        with pytest.raises(ValueError, match="needs `amount`"):
            m.run(ctx(), asset="ETH", at=AT)
