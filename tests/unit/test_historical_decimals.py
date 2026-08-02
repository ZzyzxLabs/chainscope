"""Decimals are not a constant, and treating them as one is a factor of 10^n.

Every amount here is a raw integer plus a decimals value read from the token.
For an immutable ERC-20 that is fine; for a proxy-upgradeable one the value can
change, and a contest task turns on exactly this --- it asks for a token's
*historical* decimals because the current one is wrong.

A token that moved from 8 to 18 renders every earlier transfer ten billion
times too small: small enough to read as dust and be skipped, which is worse
than an obvious error. The investigator does not see a wrong number, they see
nothing worth looking at.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chainscope.analysis.decimals import (
    DecimalsUnknown,
    TokenDecimals,
    format_at,
    resolve_at,
)

TOKEN = "0x" + "d" * 40


class TestItUsesTheValueInForce:
    def test_a_reading_applies_to_later_blocks(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        assert known.at(5000) == 8

    def test_the_nearest_earlier_reading_wins(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        known.observe(2000, 18)
        assert known.at(1500) == 8
        assert known.at(2500) == 18

    def test_a_later_reading_cannot_establish_an_earlier_value(self):
        """The case that looks harmless and is the dangerous one: the answer
        would be a real value from the wrong era."""
        known = TokenDecimals(token=TOKEN)
        known.observe(2000, 18)
        with pytest.raises(DecimalsUnknown, match="after it"):
            known.at(1000)

    def test_no_readings_at_all_refuses(self):
        with pytest.raises(DecimalsUnknown):
            TokenDecimals(token=TOKEN).at(1)


class TestAChangeIsAFinding:
    def test_it_is_detected(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        known.observe(2000, 18)
        assert known.changed

    def test_the_summary_says_what_to_do_about_it(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        known.observe(2000, 18)
        summary = known.summary()
        assert "CHANGED" in summary
        assert "needs re-checking" in summary

    def test_no_change_observed_is_not_no_change(self):
        """Only the blocks actually queried were checked, and saying so is the
        difference between a measurement and a guarantee."""
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 18)
        assert "not the same as no change" in known.summary()

    def test_the_history_is_kept_rather_than_collapsed(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        known.observe(2000, 18)
        assert known.to_dict()["readings"] == {1000: 8, 2000: 18}


class TestTheMagnitudeOfGettingItWrong:
    def test_the_same_raw_amount_under_two_regimes(self):
        """One billion base units is 10 tokens at 8 decimals and 0.000000001 at
        18. The second reads as dust."""
        known = TokenDecimals(token=TOKEN)
        known.observe(1000, 8)
        known.observe(2000, 18)
        raw = 10**9
        assert format_at(raw, TOKEN, 1500, known) == Decimal(10)
        assert format_at(raw, TOKEN, 2500, known) < Decimal("0.000001")

    def test_formatting_refuses_where_the_value_is_unknown(self):
        known = TokenDecimals(token=TOKEN)
        known.observe(2000, 18)
        with pytest.raises(DecimalsUnknown):
            format_at(10**9, TOKEN, 1000, known)

    def test_it_returns_decimal_not_float(self):
        """Wei-scale values exceed float64's exact range, and a figure printing
        as 19.999999999999996 invites distrust of arithmetic that was right."""
        known = TokenDecimals(token=TOKEN)
        known.observe(1, 18)
        assert isinstance(format_at(10**19, TOKEN, 5, known), Decimal)


class TestResolution:
    def test_it_reads_and_caches(self):
        calls = []

        def reader(token, block):
            calls.append((token, block))
            return 18

        value, known = resolve_at(TOKEN, 1000, reader)
        assert value == 18
        again, _ = resolve_at(TOKEN, 1500, reader, cache=known)
        assert again == 18
        # Second lookup satisfied from the cache: a historical eth_call is an
        # archive query and archive access is scarce.
        assert len(calls) == 1

    def test_a_provider_failure_does_not_become_a_default(self):
        """A provider that failed is not evidence of any particular value."""

        def broken(token, block):
            raise RuntimeError("archive node said no")

        with pytest.raises(DecimalsUnknown, match="Not defaulting"):
            resolve_at(TOKEN, 1000, broken)

    def test_an_earlier_query_still_reaches_the_provider(self):
        """A cached later reading must not answer for an earlier block."""
        known = TokenDecimals(token=TOKEN)
        known.observe(2000, 18)
        value, _ = resolve_at(TOKEN, 1000, lambda t, b: 8, cache=known)
        assert value == 8

    @pytest.mark.parametrize("bad", [-1, 78, 999])
    def test_an_implausible_value_is_refused(self, bad):
        """Beyond 77, 10**n stops fitting a uint256. Accepting it would let a
        hostile or misparsed token render every amount as zero."""
        with pytest.raises(ValueError, match="plausible"):
            TokenDecimals(token=TOKEN).observe(1, bad)
