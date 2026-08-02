"""Backwards tracing: what funded this balance.

The other half of the question, and the reason FIFO was chosen over haircut.
Forward tracing asks where a theft went; this asks what funded a suspect's
balance, which is what an investigator has when they start from an address
rather than an incident.

It works only because FIFO loses no information. Haircut cannot be run
backwards at all --- proportional splitting mixes every source into every
output, so you can say a balance is 3% tainted and never which 3%. That
property was the stated justification for the choice, and this is it being
used rather than asserted.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from chainscope.analysis.taint import trace_origins
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

ETH = 10**18


def move(sender, recipient, raw, block, *, decimals=18, symbol="ETH", asset=None):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.TOKEN if asset else TransferKind.NATIVE,
        block=block,
        index=0,
        asset=Address(ETHEREUM, asset, asset) if asset else None,
    )


class TestItAnswersWhatFundedThis:
    def test_a_single_source(self):
        origins = trace_origins([move("0xa", "0xb", 10 * ETH, 1)], "0xb")
        assert origins == {"0xa": 10 * ETH}

    def test_two_sources_are_kept_apart(self):
        rows = [move("0xa", "0xc", 6 * ETH, 1), move("0xb", "0xc", 4 * ETH, 2)]
        assert trace_origins(rows, "0xc") == {"0xa": 6 * ETH, "0xb": 4 * ETH}

    def test_it_follows_through_a_hop(self):
        """The value that reached C came from A, not from B --- B only passed
        it along, and reporting B would name a courier as a source."""
        rows = [move("0xa", "0xb", 10 * ETH, 1), move("0xb", "0xc", 10 * ETH, 2)]
        assert trace_origins(rows, "0xc") == {"0xa": 10 * ETH}

    def test_a_mixed_balance_splits_by_what_actually_moved(self):
        """B receives 10 from A and 10 from X, then sends 10 on. FIFO says the
        first ten left, so what C holds came from A."""
        rows = [
            move("0xa", "0xb", 10 * ETH, 1),
            move("0xx", "0xb", 10 * ETH, 2),
            move("0xb", "0xc", 10 * ETH, 3),
        ]
        assert trace_origins(rows, "0xc") == {"0xa": 10 * ETH}
        # And what B kept is the second lot.
        assert trace_origins(rows, "0xb") == {"0xx": 10 * ETH}


class TestAskingAboutPartOfABalance:
    ROWS: ClassVar = [
        move("0xa", "0xz", 5 * ETH, 1),
        move("0xb", "0xz", 5 * ETH, 2),
        move("0xc", "0xz", 5 * ETH, 3),
    ]

    def test_the_whole_balance_by_default(self):
        assert trace_origins(list(self.ROWS), "0xz") == {
            "0xa": 5 * ETH,
            "0xb": 5 * ETH,
            "0xc": 5 * ETH,
        }

    def test_the_most_recent_slice(self):
        """ "Where did the last five ETH come from" reads the back of the queue,
        since FIFO spends the front."""
        assert trace_origins(list(self.ROWS), "0xz", amount=5 * ETH) == {"0xc": 5 * ETH}

    def test_a_slice_spanning_two_lots(self):
        assert trace_origins(list(self.ROWS), "0xz", amount=7 * ETH) == {
            "0xc": 5 * ETH,
            "0xb": 2 * ETH,
        }


class TestWhatItRefusesToInvent:
    def test_an_address_with_no_history_has_no_origins(self):
        assert trace_origins([move("0xa", "0xb", ETH, 1)], "0xnobody") == {}

    def test_value_from_before_the_window_is_credited_to_its_sender(self):
        """The sender is the furthest back this data reaches. Naming it is
        honest; inventing an earlier origin would not be."""
        rows = [move("0xa", "0xb", 10 * ETH, 1)]
        assert trace_origins(rows, "0xb") == {"0xa": 10 * ETH}

    def test_assets_do_not_mix(self):
        """A USDC balance was not funded by an ETH transfer, however the
        numbers line up."""
        rows = [
            move("0xa", "0xz", 1000 * ETH, 1),
            move("0xb", "0xz", 1000 * 10**6, 2, decimals=6, symbol="USDC", asset="0xusdc"),
        ]
        assert trace_origins(rows, ("0xz", "0xusdc")) == {"0xb": 1000 * 10**6}
        assert trace_origins(rows, "0xz") == {"0xa": 1000 * ETH}

    def test_it_reports_immediate_senders_not_ultimate_ones(self):
        """Following further is the caller's decision: each hop back multiplies
        the addresses under examination, and an unbounded walk returns the
        chain's history."""
        rows = [
            move("0xorigin", "0xa", 10 * ETH, 1),
            move("0xa", "0xb", 4 * ETH, 2),
            move("0xa", "0xc", 6 * ETH, 3),
        ]
        # B's four came from A's lot, which came from origin --- and the replay
        # carries that through, so origin is named rather than A.
        assert trace_origins(rows, "0xb") == {"0xorigin": 4 * ETH}


class TestItAgreesWithForwardTracing:
    """The two directions are the same accounting read two ways. If they
    disagree the lot arithmetic is wrong somewhere."""

    @pytest.mark.parametrize("hops", [1, 2, 4])
    def test_a_chain_traces_back_to_its_start(self, hops):
        rows = []
        names = ["0xthief"] + [f"0xh{i}" for i in range(hops)]
        for i in range(hops):
            rows.append(move(names[i], names[i + 1], 10 * ETH, i + 1))
        assert trace_origins(rows, names[-1]) == {"0xthief": 10 * ETH}

    def test_a_split_apportions_to_the_same_source(self):
        rows = [
            move("0xthief", "0xa", 10 * ETH, 1),
            move("0xa", "0xb", 3 * ETH, 2),
            move("0xa", "0xc", 7 * ETH, 3),
        ]
        assert trace_origins(rows, "0xb") == {"0xthief": 3 * ETH}
        assert trace_origins(rows, "0xc") == {"0xthief": 7 * ETH}
