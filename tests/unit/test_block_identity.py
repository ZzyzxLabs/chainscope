"""The block you got back must be the block you asked for.

From a real multi-chain trace: a block number off by one hex digit returned a
different block, its timestamps put an event days from where it happened, and
the submitted answer was wrong. Nothing errored anywhere. The notes that came
out of it say to print the returned `number` and compare it against the one you
asked for, every time.

The conversion in this code cannot be mistyped. The *response* can still be the
wrong one --- a cache entry scoped too loosely, or crossed JSON-RPC batch ids,
which the same notes warn about separately. A wrong timestamp is
indistinguishable from a right one everywhere downstream, so the check belongs
at the point the block arrives.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.providers.base import ProviderError
from chainscope.providers.jsonrpc import JsonRpcProvider


class Stub(JsonRpcProvider):
    def __init__(self, reply):
        super().__init__("https://node.invalid", ETHEREUM)
        self.reply = reply
        self.asked = None

    def _call(self, method, params=None, volatility=None):
        self.asked = params[0] if params else None
        return self.reply


def block(number, *, timestamp=1700000000):
    return {
        "number": hex(number),
        "hash": "0x" + "a" * 64,
        "timestamp": hex(timestamp),
        "transactions": [],
        "parentHash": "0x" + "b" * 64,
    }


class TestTheBlockIsTheOneRequested:
    def test_a_matching_block_is_returned(self):
        p = Stub(block(20876684))
        assert p.get_block(ETHEREUM, 20876684).number == 20876684

    def test_the_request_is_hex_encoded_by_the_code_not_by_hand(self):
        """Computed, not written out. Hand-converting the expected value here
        produced 0x13e8d0c against the correct 0x13e8d8c on the first attempt
        --- the same slip the rule exists to prevent, made while writing the
        test for it."""
        p = Stub(block(20876684))
        p.get_block(ETHEREUM, 20876684)
        assert p.asked == hex(20876684)
        assert int(p.asked, 16) == 20876684

    def test_a_different_block_is_refused(self):
        """The recorded failure: 0x13E198C instead of 0x13E8D0C."""
        p = Stub(block(0x13E198C))
        with pytest.raises(ProviderError, match="returned"):
            p.get_block(ETHEREUM, 20876684)

    def test_the_error_names_both_numbers(self):
        p = Stub(block(999))
        with pytest.raises(ProviderError) as exc:
            p.get_block(ETHEREUM, 20876684)
        assert "20876684" in str(exc.value) and "999" in str(exc.value)

    def test_it_says_why_it_refuses_rather_than_coping(self):
        """Returning the wrong block's timestamp would look completely normal
        to everything downstream."""
        p = Stub(block(999))
        with pytest.raises(ProviderError, match="indistinguishable"):
            p.get_block(ETHEREUM, 20876684)

    def test_off_by_one_is_caught(self):
        p = Stub(block(20876685))
        with pytest.raises(ProviderError):
            p.get_block(ETHEREUM, 20876684)

    @pytest.mark.parametrize("tag", ["latest", "pending", "finalized"])
    def test_a_tag_is_not_compared_to_a_number(self, tag):
        """ "latest" resolves to whatever the tip is; comparing it to a literal
        would refuse every valid call."""
        p = Stub(block(20876684))
        assert p.get_block(ETHEREUM, tag).number == 20876684

    def test_a_missing_block_still_raises_not_found(self):
        with pytest.raises(ProviderError, match="not found"):
            Stub(None).get_block(ETHEREUM, 1)

    def test_a_reply_with_no_number_is_refused(self):
        """It cannot be verified, so it is not usable."""
        bad = block(1)
        del bad["number"]
        with pytest.raises(ProviderError):
            Stub(bad).get_block(ETHEREUM, 20876684)
