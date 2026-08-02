"""One-time relays: received once, sent it onward, stopped.

The shape recorded across several traces --- twelve one-time deposit addresses
into one exchange, twenty-one into a secondary relay. An operator creates an
address, funds it, sweeps it, abandons it. Cheap to do and hard to avoid
leaving.

The rule is brittle on purpose. "Few transfers" admits every quiet wallet in
the data; "exactly two" tests the property that actually matters, which is that
the address was never reused.
"""

from __future__ import annotations

from chainscope.analysis.funding import RELAY_RESIDUE_BPS, find_pass_throughs
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

ETH = 10**18


def move(sender, recipient, raw, block, *, asset=None, decimals=18, symbol="ETH"):
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


class TestItFindsTheShape:
    def test_a_clean_relay(self):
        rows = [move("0xop", "0xrelay", 100 * ETH, 1), move("0xrelay", "0xexch", 100 * ETH, 2)]
        found = find_pass_throughs(rows)
        assert [r.address for r in found] == ["0xrelay"]
        assert found[0].funder == "0xop"
        assert found[0].payee == "0xexch"

    def test_gas_dust_left_behind_still_counts(self):
        """A relay pays gas from the same balance. Requiring an exact zero
        misses most real ones."""
        rows = [
            move("0xop", "0xrelay", 100 * ETH, 1),
            move("0xrelay", "0xexch", 9999 * ETH // 100, 2),
        ]
        assert find_pass_throughs(rows)

    def test_a_meaningful_remainder_does_not(self):
        rows = [move("0xop", "0xrelay", 100 * ETH, 1), move("0xrelay", "0xexch", 50 * ETH, 2)]
        assert find_pass_throughs(rows) == []

    def test_the_recorded_fan_out(self):
        """Twelve one-time addresses funded from one place, each sweeping to
        the same exchange."""
        rows = []
        for i in range(12):
            relay = f"0xdep{i:02d}"
            rows.append(move("0xop", relay, (i + 1) * 10 * ETH, i * 2 + 1))
            rows.append(move(relay, "0xexch", (i + 1) * 10 * ETH, i * 2 + 2))
        assert len(find_pass_throughs(rows)) == 12

    def test_results_are_ordered_by_size(self):
        rows = [
            move("0xop", "0xsmall", 1 * ETH, 1),
            move("0xsmall", "0xe", 1 * ETH, 2),
            move("0xop", "0xbig", 100 * ETH, 3),
            move("0xbig", "0xe", 100 * ETH, 4),
        ]
        assert [r.address for r in find_pass_throughs(rows)] == ["0xbig", "0xsmall"]


class TestWhatItRefuses:
    def test_a_reused_address_is_not_a_relay(self):
        """Three transfers means it was used again, and reuse is the property
        being tested for."""
        rows = [
            move("0xop", "0xa", 10 * ETH, 1),
            move("0xa", "0xb", 10 * ETH, 2),
            move("0xop", "0xa", 10 * ETH, 3),
        ]
        assert find_pass_throughs(rows) == []

    def test_receiving_only_is_not_a_relay(self):
        rows = [move("0xop", "0xa", 10 * ETH, 1), move("0xop", "0xb", 10 * ETH, 2)]
        assert "0xa" not in [r.address for r in find_pass_throughs(rows)]

    def test_sending_before_receiving_is_not_a_relay(self):
        """That is an address with a balance we never watched arrive, which is
        a different thing and should not be described as a one-hop step."""
        rows = [move("0xa", "0xb", 10 * ETH, 1), move("0xop", "0xa", 10 * ETH, 2)]
        assert "0xa" not in [r.address for r in find_pass_throughs(rows)]

    def test_sending_more_than_arrived_is_not_a_relay(self):
        rows = [move("0xop", "0xa", 10 * ETH, 1), move("0xa", "0xb", 20 * ETH, 2)]
        assert find_pass_throughs(rows) == []

    def test_two_assets_are_two_histories(self):
        """An address relaying ETH while holding USDC is still a relay for the
        ETH. Merging the histories would make it look reused."""
        rows = [
            move("0xop", "0xa", 10 * ETH, 1),
            move("0xa", "0xb", 10 * ETH, 2),
            move("0xx", "0xa", 5 * 10**6, 3, asset="0xusdc", decimals=6, symbol="USDC"),
        ]
        assert [r.address for r in find_pass_throughs(rows)] == ["0xa"]

    def test_the_residue_allowance_is_what_the_module_documents(self):
        assert RELAY_RESIDUE_BPS == 100


class TestTheClaim:
    def _relay(self):
        rows = [move("0xop", "0xrelay", 100 * ETH, 1), move("0xrelay", "0xexch", 100 * ETH, 2)]
        return find_pass_throughs(rows)[0]

    def test_it_caps_at_medium(self):
        assert self._relay().attribution(ETHEREUM).confidence <= Confidence.MEDIUM

    def test_it_says_the_hop_was_deliberate_and_not_who_made_it(self):
        """An exchange generating a deposit address per customer produces
        exactly this shape."""
        rationale = self._relay().attribution(ETHEREUM).rationale
        assert "deliberate" in rationale
        assert "nothing about who" in rationale

    def test_it_reports_the_residue(self):
        assert "0.00%" in self._relay().summary()

    def test_the_claim_carries_a_source(self):
        assert self._relay().attribution().source
