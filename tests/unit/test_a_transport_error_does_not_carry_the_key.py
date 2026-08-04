"""An endpoint URL is not safe to put in an error message.

RPC providers routinely keep the credential in the *path* --- ``/v2/<key>`` for
Alchemy, ``/eth/<key>`` for Ankr --- and `str(httpx.HTTPStatusError)` includes
the full request URL. That string became a `ProviderError`, which became a row
in the read log, which is rendered on the case page for whoever is looking at
it.

Found while diagnosing a 400 from Alchemy on BSC: the key was sitting in the
`detail` column of the activity table. The scrubber already existed for this
and simply was not reached from the retry-exhausted path.
"""

from __future__ import annotations

import httpx
import pytest

from chainscope.transport.credentials import PLACEHOLDER, forget_secret, register_secret
from chainscope.transport.http import Client, TransportError

KEY = "alch_notarealkey_0123456789"
URL = f"https://bnb-mainnet.g.alchemy.com/v2/{KEY}"


def _stubbed(handler: object) -> Client:
    """A `Client` whose socket is a mock. `Client` builds its own httpx client
    lazily, so the stub is installed on the attribute it would have filled."""
    client = Client(max_retries=1, cache=None)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return client


@pytest.fixture
def registered() -> object:
    register_secret(KEY)
    register_secret(URL)
    yield
    forget_secret(KEY)
    forget_secret(URL)


def test_the_key_is_not_in_the_message(registered: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "10 block range"}})

    client = _stubbed(handler)
    with pytest.raises(TransportError) as caught:
        client.rpc(URL, "eth_getLogs", [{}])

    message = str(caught.value)
    assert KEY not in message
    assert PLACEHOLDER in message


def test_the_servers_own_explanation_survives(registered: object) -> None:
    """Scrubbing must not cost the diagnostic. The body is the useful part."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Under the Free tier plan, up to a 10 block range"}},
        )

    client = _stubbed(handler)
    with pytest.raises(TransportError) as caught:
        client.rpc(URL, "eth_getLogs", [{}])
    assert "10 block range" in str(caught.value)
