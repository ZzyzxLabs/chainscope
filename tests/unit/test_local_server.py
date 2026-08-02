"""The loopback API the browser extension talks to.

The security model is what these tests are mostly about, because getting it
wrong here is worse than not shipping it. A server on 127.0.0.1 is reachable by
every open tab: `fetch("http://127.0.0.1:8787/…")` from any page works, and the
browser makes the request happily. Binding to loopback keeps other machines out
and does nothing at all about this one.

So the token is the actual control, and it has to hold on reads as well as
writes --- which addresses somebody has been looking at is itself sensitive.
"""

import json
import urllib.error
import urllib.request

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.server.local import LocalServer, ServerOptions
from chainscope.store.sqlite import SqliteStore

pytestmark = pytest.mark.network  # binds a loopback socket

A = "0x" + "a" * 40
B = "0x" + "b" * 40
TEN_ETH = 10 * 10**18


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "s.db"
    s = SqliteStore(path)
    s.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, A, A),
                recipient=Address(ETHEREUM, B, B),
                amount=Amount(TEN_ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=100 + i,
                index=i,
            )
            for i in range(3)
        ],
        source="t",
    )
    s.put_attributions(
        [
            Attribution(
                label="Binance 14",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LABEL,
                source="etherscan",
                address=A,
                chain=ETHEREUM,
            ),
            Attribution(
                label="PancakeSwap",
                category=Category.DEX,
                confidence=Confidence.HIGH,
                method=Method.LABEL,
                source="bscscan",
                address=A,
                chain=BSC,
            ),
        ]
    )
    s.close()
    return path


@pytest.fixture
def server(store_path):
    s = LocalServer(ServerOptions(store=store_path, port=0, writable=True)).start()
    yield s
    s.stop()


@pytest.fixture
def readonly(store_path):
    s = LocalServer(ServerOptions(store=store_path, port=0, writable=False)).start()
    yield s
    s.stop()


def get(server, path, token=None, origin=None):
    request = urllib.request.Request(f"{server.url}{path}")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if origin:
        request.add_header("Origin", origin)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read()), dict(response.headers)


def post(server, path, payload, token=None):
    request = urllib.request.Request(
        f"{server.url}{path}", data=json.dumps(payload).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


class TestTheTokenIsTheControl:
    def test_no_token_is_refused(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/resolve?address=" + A)
        assert exc.value.code == 401

    def test_a_wrong_token_is_refused(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/resolve?address=" + A, token="not-the-token")
        assert exc.value.code == 401

    def test_reads_need_it_too(self, server):
        """Which addresses somebody has been looking at is an investigation
        artefact, not public information."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/flows?address=" + A)
        assert exc.value.code == 401

    def test_writes_need_it(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(server, "/tag", {"address": B, "label": "x"})
        assert exc.value.code == 401

    def test_a_fresh_token_is_generated_per_run(self, store_path):
        a = ServerOptions(store=store_path)
        b = ServerOptions(store=store_path)
        assert a.token != b.token
        assert len(a.token) >= 32

    def test_the_right_token_works(self, server):
        status, body, _ = get(server, "/resolve?address=" + A, token=server.options.token)
        assert status == 200
        assert body["claims"]


class TestCors:
    def test_an_extension_origin_is_echoed(self, server):
        _, _, headers = get(
            server,
            "/health",
            token=server.options.token,
            origin="chrome-extension://abcdef",
        )
        assert headers.get("Access-Control-Allow-Origin") == "chrome-extension://abcdef"

    def test_a_web_page_origin_gets_nothing(self, server):
        """Without the header the browser refuses to hand the response to the
        page, even though the request itself succeeded."""
        _, _, headers = get(
            server, "/health", token=server.options.token, origin="https://evil.example"
        )
        assert "Access-Control-Allow-Origin" not in headers

    def test_credentials_are_never_allowed(self, server):
        """There is no ambient session to ride, and advertising one invites an
        attempt."""
        _, _, headers = get(
            server, "/health", token=server.options.token, origin="chrome-extension://x"
        )
        assert "Access-Control-Allow-Credentials" not in headers


class TestReads:
    def test_claims_carry_provenance(self, server):
        _, body, _ = get(server, f"/resolve?address={A}&chain=1", token=server.options.token)
        claim = body["claims"][0]
        assert claim["source"] == "etherscan"
        assert claim["confidence"] == "HIGH"

    def test_another_chains_claim_is_filtered_out(self, server):
        """The same twenty bytes exist on every EVM network, so a BSC claim
        says nothing about the Ethereum address sharing its hex."""
        _, body, _ = get(server, f"/resolve?address={A}&chain=1", token=server.options.token)
        assert {c["label"] for c in body["claims"]} == {"Binance 14"}

    def test_without_a_chain_every_claim_is_returned(self, server):
        _, body, _ = get(server, f"/resolve?address={A}", token=server.options.token)
        assert len(body["claims"]) == 2

    def test_absence_is_described(self, server):
        _, body, _ = get(server, "/resolve?address=0x" + "9" * 40, token=server.options.token)
        assert body["claims"] == []
        assert "not evidence" in body["note"]

    def test_flow_totals_are_strings(self, server):
        """30 ETH exceeds what a JSON number holds exactly."""
        _, body, _ = get(server, f"/flows?address={A}&chain=1", token=server.options.token)
        total = body["flows"][0]["total_raw"]
        assert isinstance(total, str)
        assert int(total) == TEN_ETH * 3

    def test_flows_carry_decimals(self, server):
        _, body, _ = get(server, f"/flows?address={A}&chain=1", token=server.options.token)
        assert body["flows"][0]["decimals"] == 18
        assert body["flows"][0]["symbol"] == "ETH"

    def test_a_malformed_chain_is_refused(self, server):
        """Treating it as unspecified would silently widen the query rather
        than narrowing it."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, f"/resolve?address={A}&chain=bsc", token=server.options.token)
        assert exc.value.code == 400

    def test_an_unknown_route_is_a_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(server, "/nope", token=server.options.token)
        assert exc.value.code == 404


class TestWrites:
    def test_a_label_is_recorded(self, server, store_path):
        status, body = post(
            server,
            "/tag",
            {"address": B, "label": "eXch deposit", "category": "cex", "confidence": "high"},
            token=server.options.token,
        )
        assert status == 200
        assert body["recorded"]["label"] == "eXch deposit"

        s = SqliteStore(store_path)
        try:
            assert any(c.label == "eXch deposit" for c in s.attributions(B))
        finally:
            s.close()

    def test_the_source_identifies_the_browser(self, server, store_path):
        """A human reading the store later has to be able to tell where a claim
        came from, and that is not recoverable after the fact."""
        post(
            server,
            "/tag",
            {"address": B, "label": "from the page", "category": "cex"},
            token=server.options.token,
        )
        s = SqliteStore(store_path)
        try:
            claim = next(c for c in s.attributions(B) if c.label == "from the page")
        finally:
            s.close()
        assert claim.source.startswith("browser:")
        assert claim.method is Method.MANUAL

    def test_a_weak_claim_without_a_rationale_is_refused(self, server):
        """The type system's rule, surfaced where the reasoning is still in
        somebody's head."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(
                server,
                "/tag",
                {"address": B, "label": "maybe", "confidence": "low"},
                token=server.options.token,
            )
        assert exc.value.code == 400
        assert "rationale" in exc.value.read().decode()

    def test_a_weak_claim_with_a_rationale_is_accepted(self, server):
        status, _ = post(
            server,
            "/tag",
            {
                "address": B,
                "label": "maybe a mixer",
                "category": "mixer",
                "confidence": "low",
                "rationale": "equal-value outputs, no change address",
            },
            token=server.options.token,
        )
        assert status == 200

    def test_a_read_only_server_refuses_writes(self, readonly):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(
                readonly,
                "/tag",
                {"address": B, "label": "x"},
                token=readonly.options.token,
            )
        assert exc.value.code == 403

    def test_malformed_json_is_a_400_not_a_traceback(self, server):
        request = urllib.request.Request(f"{server.url}/tag", data=b"{not json", method="POST")
        request.add_header("Authorization", f"Bearer {server.options.token}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 400

    def test_an_oversized_body_is_refused_unread(self, server):
        """An unbounded read from a socket anyone on this machine can open is a
        memory-exhaustion primitive."""
        request = urllib.request.Request(f"{server.url}/tag", data=b"{}", method="POST")
        request.add_header("Authorization", f"Bearer {server.options.token}")
        request.add_header("Content-Length", str(10 * 1024 * 1024))
        with pytest.raises((urllib.error.HTTPError, OSError)):
            urllib.request.urlopen(request, timeout=5)


class TestBinding:
    def test_it_binds_loopback_by_default(self):
        assert ServerOptions().host == "127.0.0.1"

    def test_health_reports_what_the_extension_needs(self, server):
        _, body, _ = get(server, "/health", token=server.options.token)
        assert body["writable"] is True
        assert "cex" in body["categories"]
        assert "medium" in body["confidences"]
