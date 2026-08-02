"""Every declared capability must be implemented, on every provider.

Three providers declared `Capability.TRANSACTION` and inherited the base
refusal --- Sui, Blockscout, then Etherscan. Each was found separately, by
somebody calling it and getting "does not provide transactions" from a provider
the router had chosen *because* of the declaration.

`Capability`'s own docstring says why that is the expensive shape: "Overstating
is worse than omitting: the router will select you, the call returns partial
data, and an analyzer draws a conclusion from an incomplete picture."

Found three times means the next one is not going to be found by reading. This
checks every provider against every capability it claims.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ChainId
from chainscope.providers.base import Capability, Provider, ReadOnlyProvider

#: The method a capability promises, where the base class refuses by default.
PROMISED = {
    Capability.TRANSACTION: "get_transaction",
    Capability.BALANCE: "get_account",
    Capability.ADDRESS_HISTORY: "address_history",
    Capability.ASSET_TRANSFERS: "asset_transfers",
    Capability.LOGS: "get_logs",
    Capability.BLOCK: "get_block",
}


def providers() -> list[tuple[str, type[Provider], ChainId]]:
    from chainscope.providers.blockscout import BlockscoutProvider
    from chainscope.providers.etherscan import EtherscanProvider
    from chainscope.providers.jsonrpc import JsonRpcProvider
    from chainscope.providers.sui import SuiProvider

    eth = ChainId.parse("eip155:1")
    sui = ChainId.parse("sui:mainnet")
    return [
        ("etherscan", EtherscanProvider, eth),
        ("blockscout", BlockscoutProvider, eth),
        ("jsonrpc", JsonRpcProvider, eth),
        ("sui", SuiProvider, sui),
    ]


@pytest.mark.parametrize(
    ("name", "cls", "chain"), providers(), ids=lambda v: getattr(v, "__name__", str(v))
)
def test_every_declared_capability_is_implemented(name, cls, chain) -> None:
    instance = _build(cls, chain)
    # `capabilities` is a method on some providers and an attribute on others,
    # which is its own small inconsistency --- handled rather than assumed.
    declared = instance.capabilities
    if callable(declared):
        declared = declared(chain)
    for capability, method in PROMISED.items():
        if not (declared & capability):
            continue
        own = getattr(type(instance), method, None)
        base = getattr(ReadOnlyProvider, method, None) or getattr(Provider, method, None)
        assert own is not None and own is not base, (
            f"{name} declares {capability.name} and inherits {method} from the "
            f"base class, which refuses. The router reads the declaration, so "
            f"it will pick this provider and the call will fail at the point of "
            f"use --- with a message about a different problem."
        )


def _build(cls, chain):
    import inspect

    kwargs = {}
    params = inspect.signature(cls.__init__).parameters
    if "api_key" in params:
        kwargs["api_key"] = "x"
    if "chain" in params:
        kwargs["chain"] = chain
    if "url" in params:
        kwargs["url"] = "http://localhost"
    kwargs["client"] = None
    return cls(**kwargs)
