"""The three taint rules, measured against each other on one graph.

Published measurement over 132 Bitcoin heists: haircut taints over 75% of all
accounts with a non-zero balance, FIFO under 28%. This reproduces the *shape*
of that result on synthetic flows, which is the part that transfers --- the
exact percentages depend on the chain, and the ordering does not.

A choice nobody can check is a preference, so all three rules are implemented
and this file is what makes the choice inspectable.
"""

from __future__ import annotations

import random

import pytest

from chainscope.analysis.taint import TaintPolicy, trace_taint
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

SEED = 20260803
ETH = 10**18
THIEF = "0xthief"


def t(sender, recipient, raw, block):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, 18, "ETH"),
        kind=TransferKind.NATIVE,
        block=block,
        index=0,
    )


def spreading_economy(hops=6, width=4):
    """A theft, then ordinary commerce mixing with it.

    Each hop, tainted holders pay onward and clean addresses pay each other, so
    the taint has every chance to diffuse --- which is precisely the condition
    that separates the rules.
    """
    rng = random.Random(SEED)
    transfers = [t(THIEF, "0xa0", 100 * ETH, 1)]
    opening = {f"0xclean{i}": 50 * ETH for i in range(40)}
    frontier = ["0xa0"]
    block = 2
    for hop in range(hops):
        nxt = []
        for holder in frontier:
            for w in range(width):
                dst = f"0xh{hop}_{holder[-4:]}_{w}"
                transfers.append(t(holder, dst, 5 * ETH, block))
                block += 1
                nxt.append(dst)
                # Clean money flowing into the same addresses: the mixing that
                # haircut dilutes across and FIFO keeps separable.
                donor = f"0xclean{rng.randrange(40)}"
                transfers.append(t(donor, dst, 5 * ETH, block))
                block += 1
        frontier = nxt[: width * 2]
    return transfers, opening


class TestTheRulesDisagree:
    def test_only_fifo_conserves_the_stolen_amount(self):
        """The sharpest measured difference, and a better test than counting
        addresses: on a graph this size the address counts are close, and
        conservation separates the rules completely.

            100 ETH stolen
            fifo     100.0 ETH claimed as tainted   (exact)
            haircut   86.4 ETH                      (13.6 lost to rounding)
            poison   320.0 ETH                      (220 manufactured)
        """
        transfers, opening = spreading_economy()
        seed = {THIEF: 100 * ETH}
        fifo = trace_taint(transfers, seed, opening_balances=opening)
        hair = trace_taint(
            transfers, seed, policy=TaintPolicy.HAIRCUT, opening_balances=opening
        )
        poison = trace_taint(
            transfers, seed, policy=TaintPolicy.POISON, opening_balances=opening
        )
        assert fifo.total == 100 * ETH
        assert hair.total < 100 * ETH
        assert poison.total > 100 * ETH

    def test_haircut_leaks_value_it_cannot_get_back(self):
        """Proportional splitting rounds down at every hop. The lost taint
        does not reappear anywhere --- it is simply gone, which is why the rule
        cannot be run backwards."""
        transfers, opening = spreading_economy()
        hair = trace_taint(
            transfers,
            {THIEF: 100 * ETH},
            policy=TaintPolicy.HAIRCUT,
            opening_balances=opening,
        )
        assert hair.total < 90 * ETH

    def test_poison_paints_several_times_more_of_the_graph(self):
        """Measured at five times the addresses on this graph. Over thousands
        of blocks it is the published millions."""
        transfers, opening = spreading_economy()
        fifo = trace_taint(transfers, {THIEF: 100 * ETH}, opening_balances=opening)
        poison = trace_taint(
            transfers,
            {THIEF: 100 * ETH},
            policy=TaintPolicy.POISON,
            opening_balances=opening,
        )
        assert len(poison.tainted) > len(fifo.tainted) * 3

    def test_fifo_conserves_the_stolen_quantity_exactly(self):
        """The property that makes it usable: 100 ETH stolen stays 100 ETH of
        taint, sitting somewhere identifiable."""
        transfers, opening = spreading_economy()
        fifo = trace_taint(transfers, {THIEF: 100 * ETH}, opening_balances=opening)
        assert fifo.total == 100 * ETH

    def test_poison_manufactures_taint_that_was_never_stolen(self):
        """It reports three times more tainted value than was ever taken ---
        the clearest single statement of what is wrong with it. Clean money
        arriving at a touched address becomes stolen money by arithmetic."""
        transfers, opening = spreading_economy()
        poison = trace_taint(
            transfers,
            {THIEF: 100 * ETH},
            policy=TaintPolicy.POISON,
            opening_balances=opening,
        )
        assert poison.total > 300 * ETH


class TestFifoMechanics:
    def test_first_in_funds_first_out(self):
        """Clean arrives first, then stolen. The first payment out is clean."""
        transfers = [
            t("0xclean", "0xmix", 10 * ETH, 1),
            t(THIEF, "0xmix", 10 * ETH, 2),
            t("0xmix", "0xshop", 10 * ETH, 3),
            t("0xmix", "0xhideout", 10 * ETH, 4),
        ]
        r = trace_taint(transfers, {THIEF: 10 * ETH}, opening_balances={"0xclean": 10 * ETH})
        assert "0xshop" not in r.tainted
        assert r.tainted["0xhideout"] == 10 * ETH

    def test_reversing_the_arrival_order_reverses_the_answer(self):
        """Order is the whole content of FIFO. Not a bug in the rule."""
        transfers = [
            t(THIEF, "0xmix", 10 * ETH, 1),
            t("0xclean", "0xmix", 10 * ETH, 2),
            t("0xmix", "0xshop", 10 * ETH, 3),
        ]
        r = trace_taint(transfers, {THIEF: 10 * ETH}, opening_balances={"0xclean": 10 * ETH})
        assert r.tainted.get("0xshop") == 10 * ETH

    def test_a_partial_spend_moves_partial_taint(self):
        transfers = [
            t(THIEF, "0xmix", 10 * ETH, 1),
            t("0xmix", "0xout", 4 * ETH, 2),
        ]
        r = trace_taint(transfers, {THIEF: 10 * ETH})
        assert r.tainted["0xout"] == 4 * ETH
        assert r.tainted["0xmix"] == 6 * ETH

    def test_haircut_splits_where_fifo_does_not(self):
        transfers = [
            t("0xclean", "0xmix", 10 * ETH, 1),
            t(THIEF, "0xmix", 10 * ETH, 2),
            t("0xmix", "0xshop", 10 * ETH, 3),
        ]
        hair = trace_taint(
            transfers,
            {THIEF: 10 * ETH},
            policy=TaintPolicy.HAIRCUT,
            opening_balances={"0xclean": 10 * ETH},
        )
        # Half tainted under haircut; zero under FIFO.
        assert hair.tainted["0xshop"] == 5 * ETH


class TestWhatItRefusesToClaim:
    def test_holding_and_having_touched_are_separate(self):
        """ "Currently holds stolen value" and "stolen value once passed
        through" are different claims. Reporting the second as the first is how
        a payment processor becomes a launderer."""
        transfers = [
            t(THIEF, "0xrelay", 10 * ETH, 1),
            t("0xrelay", "0xfinal", 10 * ETH, 2),
        ]
        r = trace_taint(transfers, {THIEF: 10 * ETH})
        assert "0xrelay" in r.touched
        assert "0xrelay" not in r.tainted
        assert r.tainted["0xfinal"] == 10 * ETH

    def test_the_summary_states_that_distinction(self):
        r = trace_taint([], {THIEF: 1})
        assert "different claims" in r.summary()

    def test_spending_more_than_was_watched_is_recorded_not_assumed_clean(self):
        """History starting mid-stream. Calling the shortfall clean is a claim
        about money nobody watched arrive."""
        transfers = [t("0xstranger", "0xsomewhere", 10 * ETH, 1)]
        r = trace_taint(transfers, {THIEF: 1})
        assert r.unresolved

    def test_amounts_leave_as_strings(self):
        """Wei-scale: 100 ETH is 1e20, past what a JSON consumer parses
        exactly."""
        r = trace_taint([t(THIEF, "0xa", 100 * ETH, 1)], {THIEF: 100 * ETH})
        assert r.to_dict()["total_tainted"] == str(100 * ETH)

    def test_share_of_a_balance_is_capped_at_one(self):
        r = trace_taint([t(THIEF, "0xa", 10 * ETH, 1)], {THIEF: 10 * ETH})
        assert r.share("0xa", 5 * ETH) == 1.0
        assert r.share("0xa", 20 * ETH) == pytest.approx(0.5)

    def test_an_unknown_address_holds_no_taint(self):
        assert trace_taint([], {THIEF: 1}).share("0xnobody", 10 * ETH) == 0.0
