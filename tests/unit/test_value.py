"""Valuing what moved: at the time, or not at all.

`chainscope.pricing` shipped a minute-resolution rate source with a local cache
and had exactly one caller, buried inside cross-chain matching. Fourteen of the
fifty-five challenges in the reference set ask "how much was that" in some
form. That is §2 of `docs/needs.md` again --- a technique nobody can reach does
not exist --- so what is pinned here is the reachable behaviour and, more
importantly, the refusals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chainscope.cli.commands.value import value_transfers
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.pricing.base import PriceSource, Quote, RateError

WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class Rates(PriceSource):
    """Rates for the moments it was told about, and `RateError` for the rest."""

    name = "test-rates"

    def __init__(self, known: dict[datetime, str]) -> None:
        self.known = known
        self.asked: list[datetime] = []

    def rate(self, base: str, quote: str, at: datetime) -> Quote:
        self.asked.append(at)
        if at not in self.known:
            raise RateError(f"no {base}/{quote} rate for {at:%Y-%m-%d %H:%M}")
        return Quote(
            base=base,
            quote=quote,
            rate=Decimal(self.known[at]),
            at=at,
            source=self.name,
        )


def transfer(amount: str = "2", symbol: str = "ETH", at: datetime | None = WHEN) -> Transfer:
    a = Address(ETHEREUM, "0x" + "a" * 40, "0x" + "a" * 40)
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, "0x" + "1" * 64),
        sender=a,
        recipient=a,
        amount=Amount(int(Decimal(amount) * 10**18), 18, symbol),
        kind=TransferKind.NATIVE,
        timestamp=at,
        block=1,
    )


class TestItValuesAtTheMomentItMoved:
    def test_the_rate_asked_for_is_the_transfer_time(self) -> None:
        # Not now. A figure at today's rate is a different claim, and it is the
        # one a report cannot defend.
        rates = Rates({WHEN: "3000"})
        value_transfers([transfer()], rates, "USDT")
        assert rates.asked == [WHEN]

    def test_the_figure_is_amount_times_that_rate(self) -> None:
        valued, _ = value_transfers([transfer("2")], Rates({WHEN: "3000"}), "USDT")
        assert valued[0].value == Decimal("6000")

    def test_each_transfer_uses_its_own_rate(self) -> None:
        rates = Rates({WHEN: "3000", LATER: "1000"})
        valued, _ = value_transfers(
            [transfer("1", at=WHEN), transfer("1", at=LATER)], rates, "USDT"
        )
        assert [v.value for v in valued] == [Decimal("3000"), Decimal("1000")]

    def test_the_quote_travels_with_the_figure(self) -> None:
        # A number without its rate, moment and source is not defensible.
        valued, _ = value_transfers([transfer()], Rates({WHEN: "3000"}), "USDT")
        assert valued[0].quote.source == "test-rates"
        assert valued[0].quote.at == WHEN


class TestItRefusesRatherThanInterpolating:
    def test_a_missing_rate_is_a_refusal_not_the_nearest_one(self) -> None:
        """The nearest rate is usually fine and occasionally catastrophic.

        Nothing downstream can tell the two apart, which is why this refuses.
        """
        valued, refusals = value_transfers([transfer()], Rates({}), "USDT")
        assert valued == []
        assert refusals and "no ETH/USDT rate" in refusals[0]

    def test_an_undated_transfer_cannot_be_valued(self) -> None:
        # And is not valued at "now": a provider omitting a timestamp is not
        # evidence the transfer happened today.
        valued, refusals = value_transfers([transfer(at=None)], Rates({WHEN: "3000"}), "USDT")
        assert valued == []
        assert "no timestamp" in refusals[0]

    def test_a_transfer_with_no_symbol_is_refused(self) -> None:
        valued, refusals = value_transfers([transfer(symbol="")], Rates({WHEN: "3000"}), "USDT")
        assert valued == []
        assert "no asset symbol" in refusals[0]

    def test_refusals_are_returned_not_dropped(self) -> None:
        """A total over the ones that happened to price, with the rest silently
        absent, is the exact shape of a confidently wrong figure."""
        rates = Rates({WHEN: "3000"})
        valued, refusals = value_transfers(
            [transfer(at=WHEN), transfer(at=LATER), transfer(at=None)], rates, "USDT"
        )
        assert len(valued) == 1
        assert len(refusals) == 2

    def test_one_bad_rate_does_not_lose_the_good_ones(self) -> None:
        rates = Rates({LATER: "1000"})
        valued, _ = value_transfers([transfer(at=WHEN), transfer("3", at=LATER)], rates, "USDT")
        assert [v.value for v in valued] == [Decimal("3000")]


class TestExactness:
    def test_no_float_anywhere_in_the_result(self) -> None:
        # The whole package's rule. A valuation that went through a float would
        # be wrong in the last digits of every figure in a report.
        valued, _ = value_transfers([transfer("0.1")], Rates({WHEN: "3333.33"}), "USDT")
        assert isinstance(valued[0].value, Decimal)
        assert valued[0].value == Decimal("0.1") * Decimal("3333.33")

    def test_a_six_decimal_token_is_not_read_as_eighteen(self) -> None:
        a = Address(ETHEREUM, "0x" + "a" * 40, "0x" + "a" * 40)
        usdc = Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, "0x" + "2" * 64),
            sender=a,
            recipient=a,
            amount=Amount(5_000_000_000, 6, "USDC"),  # 5,000 USDC
            kind=TransferKind.TOKEN,
            timestamp=WHEN,
            block=1,
        )
        valued, _ = value_transfers([usdc], Rates({WHEN: "1"}), "USDT")
        assert valued[0].value == Decimal("5000")


class TestTheCliRefusals:
    def test_an_amount_without_a_moment_is_refused(self, capsys) -> None:
        from chainscope.cli.main import main

        assert main(["value", "10", "--symbol", "ETH"]) == 2
        assert "--at is required" in capsys.readouterr().err

    def test_nothing_at_all_is_refused(self, capsys) -> None:
        from chainscope.cli.main import main

        assert main(["value"]) == 2
        assert "give an address" in capsys.readouterr().err


class TestTheRateGapIsNeverHidden:
    """The layer underneath answers from a nearby candle when a minute is thin.

    That is right for sizing a search window and wrong to hide in a report: a
    rate stamped with the minute somebody asked about, taken from ninety
    minutes away, misstates its own provenance. This file's docstring used to
    claim the nearest rate was never used. It was.
    """

    def _quote(self, gap: int) -> Quote:
        return Quote(
            base="ETH",
            quote="USDT",
            rate=Decimal("3000"),
            at=WHEN,
            source="test-rates",
            observed_at=WHEN + timedelta(minutes=gap) if gap else None,
        )

    def test_a_rate_from_the_exact_minute_reports_no_gap(self) -> None:
        assert self._quote(0).gap_minutes == 0
        assert "observed" not in str(self._quote(0))

    def test_a_nearby_rate_says_how_far(self) -> None:
        quote = self._quote(9)
        assert quote.gap_minutes == 9
        assert "9m away" in str(quote)

    def test_the_direction_does_not_matter(self) -> None:
        assert self._quote(-9).gap_minutes == 9

    def test_a_rate_past_the_bound_is_refused(self) -> None:
        from chainscope.cli.commands.value import MAX_GAP_MINUTES

        class Far(Rates):
            def rate(self, base: str, quote: str, at: datetime) -> Quote:
                return Quote(
                    base=base,
                    quote=quote,
                    rate=Decimal("3000"),
                    at=at,
                    source="far",
                    observed_at=at + timedelta(minutes=MAX_GAP_MINUTES + 1),
                )

        valued, refusals = value_transfers([transfer()], Far({}), "USDT")
        assert valued == []
        assert "past the" in refusals[0] and "bound" in refusals[0]

    def test_a_rate_inside_the_bound_is_used(self) -> None:
        from chainscope.cli.commands.value import MAX_GAP_MINUTES

        class Near(Rates):
            def rate(self, base: str, quote: str, at: datetime) -> Quote:
                return Quote(
                    base=base,
                    quote=quote,
                    rate=Decimal("3000"),
                    at=at,
                    source="near",
                    observed_at=at + timedelta(minutes=MAX_GAP_MINUTES - 1),
                )

        valued, refusals = value_transfers([transfer()], Near({}), "USDT")
        assert len(valued) == 1 and refusals == []
        # And it still carries the distance, rather than being smoothed away.
        assert valued[0].quote.gap_minutes == MAX_GAP_MINUTES - 1

    def test_the_bound_is_tighter_than_the_sources_own(self) -> None:
        # The source's 120 minutes was chosen for search-window sizing, where
        # being an hour out costs a wider search. Here the number goes in a
        # report.
        from chainscope.cli.commands.value import MAX_GAP_MINUTES
        from chainscope.pricing.binance import BinanceKlines

        assert BinanceKlines(":memory:").max_gap_minutes > MAX_GAP_MINUTES
