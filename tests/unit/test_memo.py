"""Memos, and the difference between writing one and being written at.

Solana's SPL Memo program attaches arbitrary bytes to a transaction, which
makes it a publishing channel: a campaign puts its C2 addresses there and
infected machines read them with a public RPC call and no infrastructure to
seize.

The trap, from a recorded campaign: one wallet sent another 0 lamports with a
memo attached, putting its own instructions into that address's history. Read
the feed as the address's own output and you attribute somebody else's
instructions to it.
"""

from __future__ import annotations

import base64

import pytest

from chainscope.analysis.memo import Memo, authored_by, decode_payload, extract_indicators

SEED = "seedwallet1111111111111111111111111111111111"
OTHER = "otherwallet111111111111111111111111111111111"


def memo(payload, *, signer=SEED, lamports=None, tx="0xt", slot=1):
    return Memo(tx=tx, signer=signer, raw=payload, slot=slot, lamports=lamports)


class TestAuthorship:
    """The distinction the module exists for."""

    def test_own_and_injected_are_separated(self):
        feed = authored_by(
            [memo("a"), memo("b", signer=OTHER), memo("c")],
            SEED,
        )
        assert len(feed.own) == 2
        assert len(feed.injected) == 1

    def test_the_injector_is_named(self):
        feed = authored_by([memo("x", signer=OTHER)], SEED)
        assert feed.injectors == [OTHER]

    def test_the_summary_says_why_it_matters(self):
        feed = authored_by([memo("x", signer=OTHER)], SEED)
        assert "somebody else's instructions" in feed.summary()

    def test_a_clean_feed_says_nothing_about_injection(self):
        assert "other" not in authored_by([memo("x")], SEED).summary()

    def test_zero_value_is_the_injection_signature(self):
        """A transfer that moves nothing is not a payment with a note; the note
        is the entire point."""
        assert memo("x", lamports=0).is_injection
        assert not memo("x", lamports=5000).is_injection

    def test_a_differently_cased_signer_is_a_different_account(self):
        """Reversed, because the premise was wrong.

        This used to assert that case "does not split an author from itself".
        That is the EVM rule: hex is a checksum, so two spellings are one
        address. Solana addresses are base58, where case is part of the value
        --- and `SEEDWALLET111…` and `seedwallet111…` are *both* valid base58
        and are different accounts.

        Folding them is the confusion this module exists to prevent. Its own
        docstring: "the cost of confusing them is naming the wrong operator."
        A memo signed by one account, attributed to another because the letters
        matched with the case removed, is that cost exactly.
        """
        feed = authored_by([memo("x", signer=SEED.upper())], SEED)
        assert feed.own == []
        assert len(feed.injected) == 1


class TestDecoding:
    def test_base64_is_decoded_and_labelled(self):
        payload = base64.b64encode(b"http://203.0.113.10:8080/gate").decode()
        text, encoding = decode_payload(payload)
        assert text == "http://203.0.113.10:8080/gate"
        assert encoding == "base64"

    def test_plain_text_is_left_alone(self):
        text, encoding = decode_payload('{"c2server": "1.2.3.4"}')
        assert encoding == "utf-8"
        assert text.startswith("{")

    def test_something_that_only_looks_like_base64_is_not_mangled(self):
        """Plenty of ordinary strings decode to bytes. Only a real payload
        decodes to something printable."""
        text, _ = decode_payload("deadbeef")
        assert text == "deadbeef"

    def test_an_undecodable_payload_is_reported_not_dropped(self):
        """An operator who switched encoding mid-campaign leaves exactly that
        trace, and a decoder that silently skips it hides the switch."""
        text, encoding = decode_payload("\x00\x01\x02")
        assert encoding == "raw"
        assert text == "\x00\x01\x02"

    def test_an_empty_memo_does_not_raise(self):
        assert decode_payload("") == ("", "raw")

    @pytest.mark.parametrize("payload", ["=", "!!!!", "A" * 3, "\udcff"])
    def test_nothing_makes_it_raise(self, payload):
        decode_payload(payload)


class TestIndicators:
    def test_it_finds_an_address_in_a_url(self):
        found = extract_indicators("http://203.0.113.10:8080/gate.php")
        assert found["ipv4"] == ["203.0.113.10"]
        assert found["urls"][0].startswith("http://203.0.113.10")

    def test_octets_are_range_checked(self):
        """A bare regex reports 999.1.1.1 and finds addresses in version
        strings."""
        assert extract_indicators("version 999.888.777.666")["ipv4"] == []

    def test_a_version_string_is_not_an_address(self):
        assert extract_indicators("build 1.2.3")["ipv4"] == []

    def test_a_json_config_yields_its_port_field(self):
        """A campaign publishing a config object puts the port in a field, and
        a regex over the serialised form finds the host and loses the port."""
        found = extract_indicators('{"c2server": "198.51.100.7", "port": 8443}')
        assert "198.51.100.7" in found["ipv4"]
        assert "8443" in found["ports"]

    def test_the_first_appearance_is_kept_first(self):
        """Sorting would lose it, and the earliest memo is usually the one that
        matters."""
        found = extract_indicators("198.51.100.7 then 203.0.113.10 then 198.51.100.7")
        assert found["ipv4"] == ["198.51.100.7", "203.0.113.10"]

    def test_duplicates_collapse(self):
        found = extract_indicators("1.1.1.1 1.1.1.1 1.1.1.1")
        assert found["ipv4"] == ["1.1.1.1"]

    def test_malformed_json_still_yields_regex_hits(self):
        found = extract_indicators('{"c2server": "203.0.113.10", ')
        assert "203.0.113.10" in found["ipv4"]

    def test_nothing_is_resolved_or_contacted(self):
        """A tool that reached out to confirm an address would announce the
        investigation to the operator. The suite blocks sockets, so this
        passing at all is the assertion."""
        assert extract_indicators("http://203.0.113.10/")["ipv4"]


class TestTheDictForm:
    def test_it_carries_the_signer_and_the_encoding(self):
        payload = base64.b64encode(b"http://203.0.113.10/").decode()
        row = memo(payload, signer=OTHER, lamports=0).to_dict()
        assert row["signer"] == OTHER
        assert row["encoding"] == "base64"
        assert row["zero_value"] is True
        assert row["indicators"]["ipv4"] == ["203.0.113.10"]
