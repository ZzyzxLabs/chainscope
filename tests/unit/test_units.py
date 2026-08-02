"""Amount must never lose a satoshi."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chainscope.core.units import Amount, AmountError, total

ETH = dict(decimals=18, symbol="ETH")
BTC = dict(decimals=8, symbol="BTC")
USDC = dict(decimals=6, symbol="USDC")


class TestConstruction:
    def test_parse_display_value(self):
        assert Amount.parse("0.5", **BTC).raw == 50_000_000
        assert Amount.parse("1", **ETH).raw == 10**18
        assert Amount.parse("1234.567890", **USDC).raw == 1_234_567_890

    def test_parse_rejects_excess_precision(self):
        # Silently truncating here would produce a wrong answer that looks right.
        with pytest.raises(AmountError, match="more than 6 decimals"):
            Amount.parse("1.0000001", **USDC)

    def test_parse_accepts_exact_boundary(self):
        assert Amount.parse("0.000001", **USDC).raw == 1

    def test_rejects_bool_as_raw(self):
        with pytest.raises(AmountError):
            Amount(True, 18)  # bool is an int subclass; must be rejected

    @pytest.mark.parametrize("decimals", [-1, 37])
    def test_rejects_absurd_decimals(self, decimals):
        with pytest.raises(AmountError):
            Amount(1, decimals)


class TestArithmetic:
    def test_add_and_subtract(self):
        a = Amount.parse("1.5", **ETH)
        b = Amount.parse("0.25", **ETH)
        assert (a + b).decimal == Decimal("1.75")
        assert (a - b).decimal == Decimal("1.25")

    def test_refuses_to_mix_assets(self):
        with pytest.raises(AmountError, match="convert first"):
            Amount.parse("1", **ETH) + Amount.parse("1", **BTC)

    def test_refuses_to_mix_decimals_of_same_symbol(self):
        # USDT is 6-decimal on Ethereum but 18-decimal on BSC. Adding them is a
        # real bug that has produced real wrong numbers.
        usdt_eth = Amount(1_000_000, 6, "USDT")
        usdt_bsc = Amount(10**18, 18, "USDT")
        with pytest.raises(AmountError):
            usdt_eth + usdt_bsc

    def test_scaling_by_int_only(self):
        assert (Amount.parse("2", **BTC) * 3).decimal == Decimal("6")
        with pytest.raises(AmountError, match="ratio"):
            Amount.parse("2", **BTC) * 1.5

    def test_ratio_is_exact(self):
        got = Amount.parse("10.33479062", **BTC)
        want = Amount.parse("10.6525", **BTC)
        # 2.98% discount, computed exactly -- this is the calibration that
        # identified an unlabelled exchange wallet in the CH08 case.
        discount = (1 - got.ratio(want)) * 100
        assert Decimal("2.98") < discount < Decimal("2.99")

    def test_ratio_by_zero_raises(self):
        with pytest.raises(AmountError, match="zero"):
            Amount.parse("1", **BTC).ratio(Amount.zero(**BTC))

    def test_total_rejects_empty(self):
        with pytest.raises(AmountError, match="empty"):
            total([])

    def test_total_sums_exactly(self):
        deposits = [
            "250",
            "210",
            "690",
            "750",
            "50",
            "784",
            "1000",
            "347",
            "1008",
            "850",
            "277",
            "1900",
        ]
        got = total([Amount.parse(d, **ETH) for d in deposits])
        assert got.raw == 8116 * 10**18  # the real CH08-F03 answer, in wei


class TestOrdering:
    def test_comparisons(self):
        assert Amount.parse("1", **BTC) < Amount.parse("2", **BTC)
        assert Amount.parse("2", **BTC) >= Amount.parse("2", **BTC)

    def test_comparison_across_assets_raises(self):
        with pytest.raises(AmountError):
            _ = Amount.parse("1", **BTC) < Amount.parse("1", **ETH)

    def test_sorting(self):
        xs = [Amount.parse(v, **BTC) for v in ("3", "1", "2")]
        assert [str(x) for x in sorted(xs)] == ["1 BTC", "2 BTC", "3 BTC"]


class TestRendering:
    def test_str_trims_trailing_zeros(self):
        assert str(Amount.parse("1.500", **ETH)) == "1.5 ETH"
        assert str(Amount.parse("2", **BTC)) == "2 BTC"

    def test_format_ungrouped_by_default(self):
        # Several submission formats reject thousands separators outright.
        assert Amount.parse("1234567", **ETH).format() == "1234567 ETH"
        assert Amount.parse("1234567", **ETH).format(grouped=True) == "1,234,567 ETH"

    def test_format_rounds_on_request(self):
        assert Amount.parse("12.3456", **BTC).format(places=2) == "12.35 BTC"


class TestProperties:
    @given(
        raw=st.integers(min_value=-(10**30), max_value=10**30),
        decimals=st.integers(min_value=0, max_value=18),
    )
    def test_raw_survives_decimal_round_trip(self, raw, decimals):
        a = Amount(raw, decimals, "X")
        assert Amount.parse(a.decimal, decimals, "X").raw == raw

    @given(
        a=st.integers(min_value=-(10**24), max_value=10**24),
        b=st.integers(min_value=-(10**24), max_value=10**24),
    )
    def test_addition_is_commutative_and_exact(self, a, b):
        x, y = Amount(a, 18, "E"), Amount(b, 18, "E")
        assert (x + y).raw == (y + x).raw == a + b

    @given(
        a=st.integers(min_value=-(10**24), max_value=10**24),
        b=st.integers(min_value=-(10**24), max_value=10**24),
    )
    def test_subtraction_inverts_addition(self, a, b):
        x, y = Amount(a, 8, "B"), Amount(b, 8, "B")
        assert (x + y - y).raw == a
