"""A default endpoint that cannot answer is worse than no default.

The Sui Foundation disabled JSON-RPC on its public fullnodes in the week of 27
July 2026. Every method this provider calls now returns::

    Method not found. JSON-RPC on public fullnodes has been deprecated.
    Please migrate to gRPC or GraphQL endpoints.

The provider defaulted to `https://fullnode.mainnet.sui.io:443` when nothing
was configured, so it registered itself, the router selected it, and every call
failed. That is the capability overstatement `Capability` warns about, arriving
as a URL rather than as a flag --- and the failure reads like an outage rather
than like a decision somebody made about a protocol.

The protocol is deprecated in the node software rather than removed, so an
operator may still serve it. A *configured* endpoint is therefore still
honoured; only the known-retired ones are refused, and the refusal says what
happened and what to do instead.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ChainId
from chainscope.providers.base import ProviderError
from chainscope.providers.sui import RETIRED, SuiProvider

SUI = ChainId.parse("sui:mainnet")


class _Settings:
    def __init__(self, url: str | None) -> None:
        self.rpc = {"sui": url} if url else {}
        self.rpc_archive: dict[str, bool] = {}
        self.credentials: dict[str, object] = {}


def test_nothing_configured_registers_nothing() -> None:
    """Rather than registering a source certain to fail. `doctor` then reports
    the chain as unconfigured, which is true and actionable."""
    assert SuiProvider.from_settings(_Settings(None), SUI) == []


def test_a_retired_endpoint_is_refused_with_the_reason() -> None:
    """Configuring it explicitly is a mistake worth naming, not honouring."""
    for url in RETIRED:
        with pytest.raises(ProviderError, match="no longer serves JSON-RPC"):
            SuiProvider.from_settings(_Settings(url), SUI)


def test_the_refusal_says_what_to_do() -> None:
    with pytest.raises(ProviderError) as caught:
        SuiProvider.from_settings(_Settings(next(iter(RETIRED))), SUI)
    said = str(caught.value)
    assert "CHAINSCOPE_RPC_SUI" in said
    assert "GraphQL" in said


def test_a_configured_endpoint_is_still_honoured() -> None:
    """Node operators may keep serving JSON-RPC. Refusing every Sui endpoint
    because the Foundation's stopped would be the opposite error."""
    made = SuiProvider.from_settings(_Settings("https://sui.example/rpc"), SUI)
    assert len(made) == 1
    assert made[0].endpoint == "https://sui.example/rpc"


def test_the_retired_set_is_not_empty() -> None:
    """A guard that guards nothing passes for the wrong reason."""
    assert RETIRED
    assert any("fullnode.mainnet.sui.io" in url for url in RETIRED)
