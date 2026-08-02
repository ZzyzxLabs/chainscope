"""Transport guarantees: cache scoping, and read-only enforcement everywhere.

Both are regressions for defects that unit tests could not have caught by
accident, because both failed *silently* --- one served another chain's answer,
the other let a write through a door nobody was watching.
"""

from typing import Any

import pytest

from chainscope.transport.cache import Cache
from chainscope.transport.http import (
    Client,
    ReadOnlyViolation,
    assert_payload_read_only,
)


@pytest.fixture
def client(tmp_path):
    return Client(cache=Cache(tmp_path / "c.sqlite"))


def echo_url(monkeypatch, client: Client) -> list[str]:
    """Make the wire answer with the URL it was called on.

    A cache collision then shows up directly in the return value: if the second
    query hands back the first query's URL, the two shared an entry.
    """
    calls: list[str] = []

    def fake_send(method_: str, url: str, key: str, volatility, provider, **kw: Any) -> Any:
        calls.append(url)
        return {"jsonrpc": "2.0", "id": 1, "result": url}

    monkeypatch.setattr(client, "_send", fake_send)
    return calls


class TestCacheScoping:
    """A cached RPC answer belongs to a chain, not to a hostname."""

    def test_same_host_different_chains_do_not_collide(self, client, monkeypatch):
        # The original defect: the key was the *host*, and endpoints of the form
        # host/<chain> are everywhere, so one eth_getCode for one address on two
        # chains hashed to a single entry --- and the second chain silently
        # received the first chain's bytecode. That is exactly the shape of
        # tracing one contract deployed at the same address on four networks.
        calls = echo_url(monkeypatch, client)
        eth = client.rpc(
            "https://rpc.example.com/eth", "eth_getCode", ["0xabc", "latest"], scope="eip155:1"
        )
        bsc = client.rpc(
            "https://rpc.example.com/bsc", "eth_getCode", ["0xabc", "latest"], scope="eip155:56"
        )

        assert eth.endswith("/eth")
        assert bsc.endswith("/bsc"), "second chain was served the first chain's cached answer"
        assert len(calls) == 2

    def test_same_scope_is_reused_across_endpoints(self, client, monkeypatch):
        # The payoff of keying on the chain rather than the URL: a recorded
        # cache replays against a different node, so a case bundle is not welded
        # to the endpoint that happened to produce it, and rotating a key
        # embedded in a URL does not throw the cache away.
        calls = echo_url(monkeypatch, client)
        first = client.rpc("https://a.example.com", "eth_blockNumber", [], scope="eip155:1")
        second = client.rpc("https://b.example.com", "eth_blockNumber", [], scope="eip155:1")

        assert first == second == "https://a.example.com"
        assert len(calls) == 1

    def test_without_a_scope_the_full_url_is_the_key(self, client, monkeypatch):
        # Unportable, but never wrong: a caller that cannot name a chain must
        # still not collide with a different path on the same host.
        calls = echo_url(monkeypatch, client)
        eth = client.rpc("https://rpc.example.com/eth", "eth_blockNumber", [])
        bsc = client.rpc("https://rpc.example.com/bsc", "eth_blockNumber", [])

        assert eth != bsc
        assert len(calls) == 2

    def test_params_still_separate_entries(self, client, monkeypatch):
        calls = echo_url(monkeypatch, client)
        client.rpc("https://rpc.example.com", "eth_getBalance", ["0xa"], scope="eip155:1")
        client.rpc("https://rpc.example.com", "eth_getBalance", ["0xb"], scope="eip155:1")
        assert len(calls) == 2

    def test_repeating_a_query_hits_the_cache(self, client, monkeypatch):
        calls = echo_url(monkeypatch, client)
        a = client.rpc("https://rpc.example.com", "eth_blockNumber", [], scope="eip155:1")
        b = client.rpc("https://rpc.example.com", "eth_blockNumber", [], scope="eip155:1")
        assert a == b
        assert len(calls) == 1


class TestReadOnlyIsEnforcedEverywhere:
    """The guarantee has to hold on every path, not just the polite one."""

    def test_rpc_blocks_a_broadcast(self, client):
        with pytest.raises(ReadOnlyViolation):
            client.rpc("https://rpc.example.com", "eth_sendRawTransaction", ["0xf86..."])

    def test_hand_built_post_is_blocked(self, client):
        # Previously open: post() never consulted the deny list, so a provider
        # author could broadcast without going through rpc() at all.
        with pytest.raises(ReadOnlyViolation):
            client.post(
                "https://rpc.example.com",
                {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": []},
            )

    def test_a_batch_hiding_one_write_is_blocked(self, client):
        # Also previously open: rpc() takes a single method, so batches --- the
        # obvious way to make eighty getCode calls in one request --- went
        # around the check entirely. One bad entry rejects the whole batch.
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": ["0xa", "latest"]},
            {"jsonrpc": "2.0", "id": 2, "method": "eth_sendRawTransaction", "params": ["0xf8"]},
        ]
        with pytest.raises(ReadOnlyViolation):
            client.post("https://rpc.example.com", batch)

    def test_explorer_proxy_broadcast_is_blocked(self, client):
        # Etherscan-family APIs really do expose broadcasting as a GET:
        # ?module=proxy&action=eth_sendRawTransaction
        with pytest.raises(ReadOnlyViolation):
            client.get(
                "https://api.example.com/api",
                {"module": "proxy", "action": "eth_sendRawTransaction", "hex": "0xf86"},
            )

    def test_a_read_batch_is_allowed(self, client, monkeypatch):
        # The guard must not become a reason to avoid batching, which is the
        # only practical way to make many small queries against a rate limit.
        sent: list[Any] = []

        def fake_send(method_: str, url: str, key: str, volatility, provider, **kw: Any) -> Any:
            sent.append(kw.get("json"))
            return [{"id": 1, "result": "0x"}]

        monkeypatch.setattr(client, "_send", fake_send)
        batch = [
            {"jsonrpc": "2.0", "id": i, "method": "eth_getCode", "params": ["0xa", "latest"]}
            for i in range(3)
        ]
        client.post("https://rpc.example.com", batch)
        assert sent and len(sent[0]) == 3

    def test_ordinary_explorer_reads_are_allowed(self, client, monkeypatch):
        monkeypatch.setattr(client, "_send", lambda *a, **kw: {"status": "1", "result": []})
        out = client.get(
            "https://api.example.com/api",
            {"module": "account", "action": "txlist", "address": "0xa"},
        )
        assert out["status"] == "1"


class TestPayloadInspection:
    def test_nested_batches_are_walked(self):
        with pytest.raises(ReadOnlyViolation):
            assert_payload_read_only([[{"method": "personal_unlockAccount"}]])

    def test_non_dict_payloads_are_ignored(self):
        assert_payload_read_only("just a string")
        assert_payload_read_only(None)
        assert_payload_read_only(42)

    def test_method_fields_that_are_not_strings_do_not_crash(self):
        assert_payload_read_only({"method": {"nested": "thing"}, "action": 7})

    @pytest.mark.parametrize(
        "blocked",
        [
            "eth_sendTransaction",
            "eth_signTypedData",
            "personal_sign",
            "miner_start",
            "admin_peers",
        ],
    )
    def test_each_blocked_family(self, blocked):
        with pytest.raises(ReadOnlyViolation):
            assert_payload_read_only({"method": blocked})


class TestCacheablePredicateCannotLoseAResponse:
    """A provider supplies `cacheable` to keep errors out of the cache. A bug in
    one must cost at most a cache entry --- turning a good 200 into an exception
    would cost the answer itself."""

    def _client(self, predicate, monkeypatch):
        from chainscope.transport.cache import Volatility
        from chainscope.transport.http import Client

        cache = Cache(":memory:")
        client = Client(cache=cache)

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {"status": "1", "result": "ok"}

        class _Http:
            def request(self, *a: Any, **kw: Any) -> _Response:
                return _Response()

        monkeypatch.setattr(client, "_http", lambda: _Http())
        return client, cache, Volatility

    def test_a_raising_predicate_still_returns_the_response(self, monkeypatch):
        def explodes(_body: Any) -> bool:
            raise RuntimeError("predicate bug")

        client, _, _ = self._client(explodes, monkeypatch)
        got = client.get("https://example.test/x", {"a": 1}, cacheable=explodes)
        assert got == {"status": "1", "result": "ok"}

    def test_a_raising_predicate_declines_the_cache_entry(self, monkeypatch):
        from chainscope.transport.cache import Volatility
        from chainscope.transport.http import _request_key

        def explodes(_body: Any) -> bool:
            raise RuntimeError("predicate bug")

        client, cache, _ = self._client(explodes, monkeypatch)
        client.get("https://example.test/y", {"a": 1}, cacheable=explodes)
        key = _request_key("GET", "https://example.test/y", {"a": 1})
        assert cache.get(key, Volatility.SLOW) is None

    def test_a_well_behaved_predicate_still_caches(self, monkeypatch):
        from chainscope.transport.cache import Volatility
        from chainscope.transport.http import _request_key

        client, cache, _ = self._client(lambda _b: True, monkeypatch)
        client.get("https://example.test/z", {"a": 1}, cacheable=lambda _b: True)
        key = _request_key("GET", "https://example.test/z", {"a": 1})
        assert cache.get(key, Volatility.SLOW) == {"status": "1", "result": "ok"}
