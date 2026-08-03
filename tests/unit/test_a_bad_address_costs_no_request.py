"""An address the chain cannot hold must be rejected before a request is spent.

`notanaddress` reached Blockscout and came back "Invalid address format" after
a network round trip --- a rate-limit slot and a second of latency spent to
learn something `ChainAdapter.is_valid` already knew. The adapter had been
there all along; the server never asked it.

Small in isolation, and the shape is the recurring one: work done remotely
that was already answerable locally.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ETHEREUM, ChainId
from chainscope.server.local import _check_address


def test_a_malformed_evm_address_is_refused() -> None:
    with pytest.raises(ValueError, match="not a valid address"):
        _check_address("notanaddress", ETHEREUM)


def test_the_message_says_nothing_was_spent() -> None:
    """So a reader knows this cost them no rate limit."""
    with pytest.raises(ValueError, match="no request was spent"):
        _check_address("0xdeadbeef", ETHEREUM)


@pytest.mark.parametrize(
    "address",
    [
        "0x098B716B8Aaf21512996dC57EB0615e2383E2f96",
        "0x098b716b8aaf21512996dc57eb0615e2383e2f96",
    ],
)
def test_real_addresses_pass_in_either_case(address: str) -> None:
    _check_address(address, ETHEREUM)


def test_an_unknown_namespace_is_not_judged() -> None:
    """Refusing an address because this build lacks its adapter would be worse
    than letting the provider answer."""
    _check_address("whatever-this-is", ChainId("madeup", "1"))
