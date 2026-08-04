"""A transfer with no symbol cannot be checked for impersonation.

The log-scanning provider first built `Transfer` rows with an empty symbol and
a hardcoded 18 decimals, on the reasoning that `Amount` carries the raw value
either way and fetching metadata costs a request per token.

It costs a request per **contract**, which is a different number --- eighty
transfers across four tokens is eight calls --- and skipping it cost the
analysis the scan existed for. `impersonation` decides whether a token is
imitating a real one by comparing its symbol against the canonical contract
for that symbol. Given no symbol it has nothing to compare, so three
counterfeit USDC contracts on BSC were reported as "assets the registry says
nothing about" rather than as forgeries. One of them was named with invisible
characters.
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.core.chainid import ChainId
from chainscope.providers.jsonrpc import JsonRpcProvider

CHAIN = ChainId.evm(56)
TOKEN = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"


def _abi_string(text: str) -> str:
    raw = text.encode()
    return (
        "0x"
        + "20".rjust(64, "0")
        + f"{len(raw):064x}"
        + raw.hex().ljust(((len(raw) + 31) // 32) * 64, "0")
    )


class _Node:
    """Counts calls so the cache can be asserted, not assumed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def rpc(self, url: str, method: str, params: Any = None, **_: Any) -> Any:
        self.calls.append((method, params))
        if method != "eth_call":
            return None
        # Answers for the known token only. A stub that replies for every
        # address cannot tell "asked once and got nothing" from "asked once".
        if params[0]["to"].lower() != TOKEN:
            return "0x"
        data = params[0]["data"]
        if data == "0x95d89b41":
            return _abi_string("USDC")
        if data == "0x313ce567":
            return "0x" + f"{18:064x}"
        return "0x"


@pytest.fixture
def provider() -> tuple[JsonRpcProvider, _Node]:
    node = _Node()
    return JsonRpcProvider("https://node.example", CHAIN, client=node), node  # type: ignore[arg-type]


def _log(token: str = TOKEN) -> dict[str, Any]:
    pad = "0x" + "0" * 24
    return {
        "address": token,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            pad + "11" * 20,
            pad + "22" * 20,
        ],
        "data": "0x" + f"{10**18:064x}",
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": "0x1",
        "logIndex": "0x0",
    }


def test_the_symbol_reaches_the_transfer(provider: tuple[JsonRpcProvider, _Node]) -> None:
    rpc, _node = provider
    made = rpc._transfer_from_log(CHAIN, _log())
    assert made is not None
    assert made.amount.symbol == "USDC"
    assert made.amount.decimals == 18


def test_each_contract_is_asked_once(provider: tuple[JsonRpcProvider, _Node]) -> None:
    """Per contract, not per transfer --- otherwise a busy token costs a call
    for every row it produced."""
    rpc, node = provider
    for _ in range(25):
        rpc._transfer_from_log(CHAIN, _log())
    assert len([c for c in node.calls if c[0] == "eth_call"]) == 2


def test_a_contract_that_will_not_answer_is_not_retried(
    provider: tuple[JsonRpcProvider, _Node],
) -> None:
    """A token with no `symbol()` is a real thing, not a transient failure."""
    rpc, node = provider
    silent = "0x" + "cd" * 20
    for _ in range(5):
        made = rpc._transfer_from_log(CHAIN, _log(silent))
    assert made is not None
    assert made.amount.symbol == ""
    assert len([c for c in node.calls if c[0] == "eth_call"]) == 2


def test_an_erc721_transfer_is_not_reported_as_an_amount(
    provider: tuple[JsonRpcProvider, _Node],
) -> None:
    """Its token id is a fourth topic and its data is empty, which would decode
    as a quantity of zero and land in every total."""
    rpc, _node = provider
    nft = _log()
    nft["topics"].append("0x" + f"{7:064x}")
    nft["data"] = "0x"
    assert rpc._transfer_from_log(CHAIN, nft) is None
