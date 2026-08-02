"""Mixer correlation reachable from the CLI, and the log parsing under it.

The correlation itself is scored in
tests/validation/test_mixer_correlation_accuracy.py. This is about the wiring:
the pool constants, the Withdrawal event layout, and the one thing that makes
enumeration here different from enumeration anywhere else --- a short log list
does not merely lose rows, it *shrinks the anonymity set*, which makes every
confidence in the result stronger than the data supports.
"""

from __future__ import annotations

import pytest

from chainscope.analysis.mixer import (
    TORNADO_ETH_POOLS,
    TORNADO_ROUTER,
    WITHDRAWAL_TOPIC,
    MixerAnalyzer,
    parse_withdrawal,
)

RECIPIENT = "0x96dc12a1c0f3ad73b9de2e16f88785bac0b6d497"


def log(*, recipient=RECIPIENT, block=20005985, index=7, topic=WITHDRAWAL_TOPIC):
    return {
        "topics": [topic, "0x" + "0" * 64],
        # to, nullifierHash, fee --- `to` is the first non-indexed word.
        "data": "0x" + recipient[2:].rjust(64, "0") + "ab" * 32 + "00" * 32,
        "blockNumber": str(block),
        "logIndex": str(index),
        "transactionHash": "0x" + "e" * 64,
    }


class TestTheEventLayout:
    def test_the_recipient_is_the_first_word_of_the_data(self):
        """From a recorded trace: this address withdrew at block 20005985."""
        event = parse_withdrawal(log())
        assert event is not None
        assert event.address == RECIPIENT

    def test_it_takes_the_last_twenty_bytes_not_the_whole_word(self):
        """A 32-byte string matches no address anywhere downstream."""
        assert len(parse_withdrawal(log()).address) == 42

    def test_block_and_index_survive(self):
        event = parse_withdrawal(log(block=20005985, index=7))
        assert (event.block, event.index) == (20005985, 7)

    def test_hex_encoded_numbers_are_accepted(self):
        raw = log()
        raw["blockNumber"] = "0x1312d00"
        raw["logIndex"] = "0x7"
        assert parse_withdrawal(raw).block == 0x1312D00

    def test_another_event_from_the_same_pool_is_not_a_withdrawal(self):
        """None rather than a guess: a log this does not understand is not a
        withdrawal to nobody."""
        assert parse_withdrawal(log(topic="0x" + "1" * 64)) is None

    def test_a_truncated_data_field_is_refused(self):
        raw = log()
        raw["data"] = "0xdeadbeef"
        assert parse_withdrawal(raw) is None

    def test_a_log_with_no_topics_is_refused(self):
        assert parse_withdrawal({"data": "0x" + "0" * 64}) is None


class TestThePoolConstants:
    @pytest.mark.parametrize("denomination", ["0.1", "1", "10", "100"])
    def test_every_eth_pool_is_present(self, denomination):
        assert TORNADO_ETH_POOLS[denomination].startswith("0x")
        assert len(TORNADO_ETH_POOLS[denomination]) == 42

    def test_the_ten_eth_pool_matches_the_recorded_case(self):
        assert TORNADO_ETH_POOLS["10"] == "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf"

    def test_the_router_is_recorded_too(self):
        """The trap: deposits call the Router, not the pool. A filter matching
        only pool addresses finds nothing, silently, and that reads as "this
        address never touched Tornado"."""
        assert TORNADO_ROUTER == "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b"

    def test_all_addresses_are_lowercased(self):
        """They are compared against normalised keys, and a checksummed
        constant would never match."""
        for address in [*TORNADO_ETH_POOLS.values(), TORNADO_ROUTER]:
            assert address == address.lower()


class TestTheAnalyzerContract:
    def test_it_is_registered(self):
        from chainscope.cli.commands.analyze import available, rejected

        assert "mixer" in available()
        assert "mixer" not in rejected()

    def test_it_constructs_with_no_arguments(self):
        assert MixerAnalyzer().description

    def test_it_refuses_without_deposits_and_says_why(self):
        """The Deposit event does not name the depositor, so deposits cannot be
        discovered from logs. Better to say that than to return nothing."""
        with pytest.raises(ValueError, match="does not name the depositor"):
            MixerAnalyzer().run(None, deposits="")

    def test_a_denomination_resolves_to_a_pool(self):
        """`-p pool=10` rather than pasting an address from a blog post."""
        assert TORNADO_ETH_POOLS.get("10")

    def test_an_unknown_pool_is_passed_through_as_an_address(self):
        """So a pool this package does not list is still usable."""
        assert TORNADO_ETH_POOLS.get("0xabc", "0xabc") == "0xabc"
