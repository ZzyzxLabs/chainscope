"""The extension's route into the store, exercised rather than assumed.

Everything here was verified by running it: `chainscope-serve` on loopback, the
three endpoints the extension actually calls, and the claim arriving in the
store where `chainscope label` reads it back.

Worth doing because this session found a self-contained HTML page whose
JavaScript had never parsed --- shipped, documented in the demo, and dead. A
surface that has only been read is a surface nobody has checked.
"""

from __future__ import annotations

import pytest

from chainscope.server.local import ServerOptions, _Handlers
from chainscope.store.sqlite import SqliteStore

USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


@pytest.fixture
def handlers(tmp_path) -> _Handlers:
    return _Handlers(
        ServerOptions(
            store=tmp_path / "store.db",
            writable=True,
            token="test",
            analyst="alice@lab",
        )
    )


class TestWhatTheExtensionCalls:
    def test_health_describes_the_store_and_the_vocabulary(self, handlers) -> None:
        # The popup reads `categories` and `confidences` to build its form, so
        # they are part of the contract, not diagnostics.
        out = handlers.health()
        assert out["ok"] is True
        assert "cex" in out["categories"]
        assert "speculative" in out["confidences"]

    def test_resolve_returns_what_the_store_knows(self, handlers) -> None:
        handlers.tag(
            {"address": USDT, "label": "Tether", "category": "token", "confidence": "high"}
        )
        out = handlers.resolve({"address": [USDT]})
        assert [c["label"] for c in out["claims"]] == ["Tether"]

    def test_tag_writes_a_claim_the_cli_can_read(self, handlers, tmp_path) -> None:
        handlers.tag(
            {"address": USDT, "label": "Tether", "category": "token", "confidence": "high"}
        )
        store = SqliteStore(tmp_path / "store.db")
        try:
            assert [c.label for c in store.attributions(USDT)] == ["Tether"]
        finally:
            store.close()


class TestTheClaimSaysWhereItCameFrom:
    def test_the_origin_marker_cannot_be_replaced(self, handlers, tmp_path) -> None:
        """Any page in the browser can reach this endpoint.

        Caller text used to *become* the source, so a request could label itself
        "OFAC SDN list" and sit in the store indistinguishable from an import of
        one.
        """
        handlers.tag(
            {
                "address": USDT,
                "label": "Tether",
                "category": "token",
                "confidence": "high",
                "source": "OFAC SDN list",
            }
        )
        store = SqliteStore(tmp_path / "store.db")
        try:
            source = store.attributions(USDT)[0].source
        finally:
            store.close()
        assert source.startswith("browser:")
        # Kept, because it is usually the useful part --- but marked as claimed.
        assert "reported: OFAC SDN list" in source

    def test_the_analyst_comes_from_the_server_not_the_request(
        self, handlers, tmp_path
    ) -> None:
        """Browser-written claims carried no analyst at all.

        `report` then filed a label a person had typed alongside bulk imports
        and heuristics. The server knows who is running it --- their machine,
        their loopback, their token --- and the request must not be able to say.
        """
        handlers.tag(
            {
                "address": USDT,
                "label": "Tether",
                "category": "token",
                "confidence": "high",
                "analyst": "somebody-else@evil",
            }
        )
        store = SqliteStore(tmp_path / "store.db")
        try:
            assert store.attributions(USDT)[0].analyst == "alice@lab"
        finally:
            store.close()

    def test_no_chosen_identity_means_no_analyst(self, tmp_path) -> None:
        # Rather than an OS account. A machine login is not authorship, and a
        # claim signed with one is worse than one signed with nothing.
        handlers = _Handlers(
            ServerOptions(store=tmp_path / "store.db", writable=True, token="t")
        )
        handlers.tag(
            {"address": USDT, "label": "Tether", "category": "token", "confidence": "high"}
        )
        store = SqliteStore(tmp_path / "store.db")
        try:
            assert store.attributions(USDT)[0].analyst == ""
        finally:
            store.close()


class TestItRefuses:
    def test_a_read_only_server_will_not_write(self, tmp_path) -> None:
        handlers = _Handlers(ServerOptions(store=tmp_path / "store.db", token="t"))
        with pytest.raises(PermissionError, match="--writable"):
            handlers.tag({"address": USDT, "label": "Tether"})

    def test_a_label_with_no_address_is_refused(self, handlers) -> None:
        with pytest.raises(ValueError, match="address and label are required"):
            handlers.tag({"label": "Tether"})

    def test_an_unknown_confidence_is_named(self, handlers) -> None:
        with pytest.raises(ValueError, match="unknown confidence"):
            handlers.tag({"address": USDT, "label": "T", "confidence": "certainly"})


class TestTheExtensionAndServerAgreeOnRoutes:
    def test_every_path_the_extension_calls_is_served(self) -> None:
        """The mismatch this file exists to rule out.

        Two halves shipped together and never met is exactly how the flow page
        went a whole release without its JavaScript parsing.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        source = "\n".join(p.read_text() for p in (root / "extension").glob("*.js"))
        called = set(re.findall(r"\$\{endpoint\}(/[a-z]+)", source))
        assert called, "found no endpoint calls in the extension"

        served = (root / "src" / "chainscope" / "server" / "local.py").read_text()
        for path in sorted(called):
            assert f'"{path}"' in served, f"extension calls {path}; server has no route"
