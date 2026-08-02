"""A claim written through the browser cannot choose its own provenance.

The loopback server is reachable by any page in the user's browser, and it
writes into the attribution store. Caller-supplied text used to *become* the
source, so a request could label a claim "OFAC SDN list" and it would sit in
the store indistinguishable from a real import of one.

Provenance that a request can pick is not provenance. `Attribution` cannot be
constructed without a source precisely so that every claim can be traced back;
letting the claim choose what it says defeats the type.

These tests do not bind a socket --- they call the handler directly --- so they
run in the default suite rather than behind the `network` marker.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.server.local import ServerOptions, _Handlers
from chainscope.store.sqlite import SqliteStore


@pytest.fixture
def handlers(tmp_path):
    SqliteStore(tmp_path / "s.db").close()
    return _Handlers(ServerOptions(store=tmp_path / "s.db", writable=True))


def tag(handlers, **body):
    body.setdefault("address", "0x" + "a" * 40)
    body.setdefault("label", "Something")
    body.setdefault("category", "cex")
    body.setdefault("confidence", "high")
    return handlers.tag(body)


class TestTheOriginMarkerSurvives:
    def test_a_bare_tag_records_the_browser(self, handlers):
        assert tag(handlers)["recorded"]["source"] == "browser:browser-extension"

    def test_supplied_text_cannot_replace_it(self, handlers):
        """The attack: a page claiming its label came from a sanctions list."""
        source = tag(handlers, source="OFAC SDN list")["recorded"]["source"]
        assert source.startswith("browser:")
        assert "OFAC SDN list" in source

    def test_supplied_text_is_marked_as_reported_not_established(self, handlers):
        assert "reported:" in tag(handlers, source="etherscan public tag")["recorded"]["source"]

    def test_the_useful_part_is_still_kept(self, handlers):
        """Discarding it would be the other failure: "etherscan public tag" is
        usually the most useful thing in the record."""
        assert (
            "etherscan public tag"
            in tag(handlers, source="etherscan public tag")["recorded"]["source"]
        )

    @pytest.mark.parametrize(
        "hostile",
        [
            "browser:browser-extension",
            "chainscope internal",
            "manual review by analyst",
            "",
            "   ",
        ],
    )
    def test_no_supplied_value_can_impersonate_the_marker(self, handlers, hostile):
        """Including one that copies the marker verbatim: it still gets the
        prefix once, from the server, not from the request."""
        source = tag(handlers, source=hostile)["recorded"]["source"]
        assert source.startswith("browser:browser-extension")

    def test_a_configured_agent_name_is_the_one_recorded(self, tmp_path):
        """Two people tagging into one store need to be tellable apart."""
        SqliteStore(tmp_path / "s.db").close()
        h = _Handlers(
            ServerOptions(store=tmp_path / "s.db", writable=True, agent_name="alice-firefox")
        )
        assert tag(h)["recorded"]["source"] == "browser:alice-firefox"


class TestItReachesTheStore:
    def test_the_claim_is_readable_afterwards(self, handlers):
        address = "0x" + "b" * 40
        tag(handlers, address=address, label="Binance 14", chain="eip155:1")
        store = SqliteStore(handlers.options.store)
        try:
            claims = list(store.attributions(address))
        finally:
            store.close()
        assert len(claims) == 1
        assert claims[0].label == "Binance 14"
        assert claims[0].chain == ETHEREUM
        assert claims[0].source.startswith("browser:")

    def test_the_method_says_a_person_did_it(self, handlers):
        """MANUAL, because somebody clicked while reading a page. Recording it
        as LIST would overstate it as a compiled source."""
        assert tag(handlers)["recorded"]
        store = SqliteStore(handlers.options.store)
        try:
            claim = next(iter(store.attributions("0x" + "a" * 40)))
        finally:
            store.close()
        assert claim.method.value == "manual"
