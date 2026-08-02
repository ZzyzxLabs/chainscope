"""Who takes a fixed cut, and who is passing through.

From a drainer-as-a-service case: each theft paid an affiliate, an operator,
and a relayer, in one transaction, out of one contract. The affiliate changes
every time and the operator's percentage does not --- that asymmetry is the
whole technique.

One transaction tells you who was paid. Many tell you what they are.
"""

from __future__ import annotations

from decimal import Decimal

from chainscope.analysis.revenue import (
    MIN_DISTRIBUTIONS,
    STABLE_TOLERANCE,
    Distribution,
    analyse_splits,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM

ETH = 10**18
OPERATOR = "0x9fa7bb759641fcd37fe4ae41f725e0f653f2c726"
RELAYER = "0xrelayer"


def drains(count=8, operator_bps=2000, rebate=None):
    """A drainer paying a fixed operator cut, a varying affiliate, and a rebate
    that has nothing to do with the size of the take."""
    out = []
    for i in range(count):
        total = (10 + i * 3) * ETH
        op = total * operator_bps // 10_000
        fee = rebate if rebate is not None else (i + 1) * ETH // 100
        out.append(
            Distribution(
                tx=f"0x{i:064x}",
                payouts={
                    # A different affiliate every time --- that is what makes
                    # the operator's constancy legible.
                    f"0xaffiliate{i:02d}": total - op - fee,
                    OPERATOR: op,
                    RELAYER: fee,
                },
            )
        )
    return out


class TestItSeparatesTheFixedCutFromTheRest:
    def test_the_operator_is_stable(self):
        by = {r.address: r for r in analyse_splits(drains())}
        assert by[OPERATOR].is_stable
        assert by[OPERATOR].percent == Decimal("20.00")

    def test_the_relayer_is_not(self):
        """A rebate tracks gas, not the size of the take."""
        by = {r.address: r for r in analyse_splits(drains())}
        assert not by[RELAYER].is_stable

    def test_each_affiliate_appears_once_and_cannot_be_judged(self):
        stable = {r.address for r in analyse_splits(drains()) if r.is_stable}
        assert not any(a.startswith("0xaffiliate") for a in stable)

    def test_the_stable_ones_come_first(self):
        """The order a reader wants: parties to an agreement before the ones
        passing through."""
        found = analyse_splits(drains())
        assert found[0].address == OPERATOR

    def test_a_non_round_percentage_is_just_as_stable(self):
        """A stable 19.37% is exactly as unlikely by chance as a stable 20.00%.
        Only a human reader finds the second more convincing."""
        by = {r.address: r for r in analyse_splits(drains(operator_bps=1937))}
        assert by[OPERATOR].is_stable

    def test_roundness_is_reported_separately(self):
        """17% is round too --- the first version of this test used it as the
        counter-example, which says something about how readily roundness
        persuades. 17.5% is the actual counter-example."""
        by_round = {r.address: r for r in analyse_splits(drains(operator_bps=2000))}
        by_odd = {r.address: r for r in analyse_splits(drains(operator_bps=1750))}
        assert by_round[OPERATOR].is_round
        assert not by_odd[OPERATOR].is_round
        # And both are equally stable, which is the whole point of separating
        # the two measures.
        assert by_round[OPERATOR].is_stable
        assert by_odd[OPERATOR].is_stable


class TestWhatItRefusesToConclude:
    def test_too_few_distributions_is_not_a_finding(self):
        """With three, two recipients splitting evenly hit the same percentage
        every time for reasons that have nothing to do with an agreement."""
        found = {r.address: r for r in analyse_splits(drains(count=MIN_DISTRIBUTIONS - 1))}
        assert not found[OPERATOR].is_stable
        assert "Too few" in found[OPERATOR].summary()

    def test_the_count_travels_so_a_reader_can_use_their_own_floor(self):
        found = {r.address: r for r in analyse_splits(drains(count=3))}
        assert found[OPERATOR].appearances == 3

    def test_an_absence_is_not_a_zero_percent_share(self):
        """Counting it would make an occasional participant look wildly
        variable when it is simply not always involved."""
        dists = [
            Distribution(tx="0x1", payouts={"0xa": 8 * ETH, "0xb": 2 * ETH}),
            Distribution(tx="0x2", payouts={"0xa": 8 * ETH, "0xb": 2 * ETH}),
            Distribution(tx="0x3", payouts={"0xa": 10 * ETH}),
            Distribution(tx="0x4", payouts={"0xa": 8 * ETH, "0xb": 2 * ETH}),
            Distribution(tx="0x5", payouts={"0xa": 8 * ETH, "0xb": 2 * ETH}),
        ]
        by = {r.address: r for r in analyse_splits(dists)}
        assert by["0xb"].appearances == 4
        assert by["0xb"].is_stable

    def test_it_does_not_name_the_role(self):
        """ "Operator" is a reading of a case. A function printing it would be
        asserting a business structure from arithmetic."""
        claim = {r.address: r for r in analyse_splits(drains())}[OPERATOR].attribution(ETHEREUM)
        assert "operator" not in claim.label.lower()
        assert "fixed" in claim.label

    def test_the_claim_says_it_does_not_identify_the_party(self):
        claim = {r.address: r for r in analyse_splits(drains())}[OPERATOR].attribution(ETHEREUM)
        assert "is the party that made it" in claim.rationale
        assert "does not say" in claim.rationale

    def test_it_caps_at_medium(self):
        claim = {r.address: r for r in analyse_splits(drains())}[OPERATOR].attribution(ETHEREUM)
        assert claim.confidence <= Confidence.MEDIUM

    def test_an_unstable_recipient_makes_no_claim(self):
        by = {r.address: r for r in analyse_splits(drains())}
        assert by[RELAYER].attribution(ETHEREUM) is None


class TestTheArithmetic:
    def test_shares_are_basis_points_of_the_whole(self):
        dist = Distribution(tx="0x1", payouts={"0xa": 3 * ETH, "0xb": 1 * ETH})
        assert dist.share_bps("0xa") == 7500

    def test_an_empty_distribution_divides_by_nothing(self):
        assert Distribution(tx="0x1", payouts={}).share_bps("0xa") == 0

    def test_percentages_are_decimal_not_float(self):
        """A share printed as 19.999999999999996 invites the reader to distrust
        the arithmetic, and this figure gets quoted."""
        by = {r.address: r for r in analyse_splits(drains())}
        assert isinstance(by[OPERATOR].percent, Decimal)

    def test_wei_rounding_stays_inside_the_tolerance(self):
        """Integer division on wei moves a nominally fixed cut by a basis point
        or two. Demanding an exact match would reject real agreements."""
        by = {r.address: r for r in analyse_splits(drains(count=12))}
        assert by[OPERATOR].is_stable
        assert Decimal(by[OPERATOR].spread_bps) / by[OPERATOR].median_bps <= STABLE_TOLERANCE

    def test_zero_value_payouts_are_not_recipients(self):
        dists = [Distribution(tx=f"0x{i}", payouts={"0xa": ETH, "0xzero": 0}) for i in range(5)]
        assert "0xzero" not in {r.address for r in analyse_splits(dists)}
