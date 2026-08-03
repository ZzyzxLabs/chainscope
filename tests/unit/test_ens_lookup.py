"""Reading ENS, which nothing had ever done.

Three modules sat in a chain with no first link. `attribution.ens` knew how to
namehash a name and how to forward-confirm a reverse record --- the rule that
separates "this address has a name" from "anybody can point a name at any
address". `osint.leads` knew how to turn a *confirmed* record's text entries
into leads. `case.leads` (added alongside this) knew how to keep them. Nothing
had ever constructed an `EnsRecord`, so none of it ran.

The tests below are about the order of operations, because the order is the
whole safety property:

* text records are fetched only *after* forward-confirmation succeeds --- an
  unconfirmed name's text records belong to whoever owns the name, and filing
  them against this address attaches another person's identity to it;
* a resolver of zero is checked before any call is made to it, because the ABI
  decoder will happily turn empty return data into an empty string, and "no
  resolver" would become "no Twitter handle";
* "not checked" and "checked, resolves nowhere" stay distinct, because
  collapsing them lets "we did not look" read as "it does not confirm".

Verified live against mainnet while being written: vitalik.eth resolves,
confirms, and yields four leads; Binance 14 has no reverse record at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.attribution.ens import _keccak, namehash, normalise_name, reverse_node
from chainscope.attribution.ens_lookup import (
    ENS_REGISTRY,
    EnsLookup,
    _decode_address,
    _decode_string,
    _encode_string_arg,
)
from chainscope.core.chainid import ETHEREUM

VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
STRANGER = "0x1111111111111111111111111111111111111111"
RESOLVER = "0x231b0ee14048e9dccd1d247744d114a4eb5e8e63"


def _word(value: str) -> str:
    return value.rjust(64, "0")


def _string(value: str) -> str:
    data = value.encode()
    padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return "0x" + _word("20") + f"{len(data):064x}" + padded.hex()


class FakeChain:
    """A provider answering `eth_call` from a script of expected calls."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def call(self, chain: Any, to: str, data: str, block: Any = "latest") -> str:
        self.calls.append((to.lower(), data[:10]))
        for prefix, answer in self.answers.items():
            if data.startswith(prefix):
                return answer
        return "0x"


class TestSelectorsMatchTheirSignatures:
    """A wrong constant here is a silent wrong call, so it is recomputed."""

    @pytest.mark.parametrize(
        ("signature", "expected"),
        [
            ("resolver(bytes32)", "0178b8bf"),
            ("name(bytes32)", "691f3431"),
            ("addr(bytes32)", "3b3b57de"),
            ("text(bytes32,string)", "59d1d43c"),
        ],
    )
    def test_selector(self, signature: str, expected: str) -> None:
        assert _keccak()(signature.encode()).hex()[:8] == expected

    def test_the_registry_address_is_the_protocol_one(self) -> None:
        assert ENS_REGISTRY == "0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e"


class TestDecoding:
    def test_an_address_is_the_last_twenty_bytes(self) -> None:
        assert _decode_address("0x" + _word(VITALIK[2:])) == VITALIK

    def test_short_data_decodes_to_nothing_rather_than_raising(self) -> None:
        assert _decode_address("0x") == ""
        assert _decode_string("0x") == ""

    def test_a_string_round_trips(self) -> None:
        assert _decode_string(_string("vitalik.eth")) == "vitalik.eth"

    def test_a_length_past_the_buffer_is_refused(self) -> None:
        # A resolver returning junk for one key must not be read as a value.
        assert _decode_string("0x" + _word("20") + _word("ffff") + "00" * 32) == ""

    def test_non_utf8_bytes_decode_to_nothing(self) -> None:
        assert _decode_string("0x" + _word("20") + _word("4") + "ff" * 32) == ""

    def test_a_string_argument_starts_after_two_words(self) -> None:
        # Offset 0x40: the first argument is the 32-byte node.
        assert _encode_string_arg("url").startswith(f"{64:064x}")


class TestTheOrderOfOperations:
    def _lookup(self, answers: dict[str, str]) -> tuple[EnsLookup, FakeChain]:
        chain = FakeChain(answers)
        return EnsLookup(chain, ETHEREUM), chain

    def _confirmed(self) -> dict[str, str]:
        return {
            "0x0178b8bf": "0x" + _word(RESOLVER[2:]),
            "0x691f3431": _string("vitalik.eth"),
            "0x3b3b57de": "0x" + _word(VITALIK[2:]),
            "0x59d1d43c": _string("VitalikButerin"),
        }

    def test_a_confirmed_record_produces_leads(self) -> None:
        look, _ = self._lookup(self._confirmed())
        found = look.look_up(VITALIK)
        assert found.confirmed
        assert found.leads

    def test_an_unconfirmed_record_produces_none(self) -> None:
        answers = self._confirmed()
        answers["0x3b3b57de"] = "0x" + _word(STRANGER[2:])
        look, _ = self._lookup(answers)
        found = look.look_up(VITALIK)
        assert not found.confirmed
        assert found.leads == []

    def test_and_its_text_records_are_never_even_fetched(self) -> None:
        """The safety property, checked at the wire rather than in the result.

        Filtering after the fetch would leave another person's handles in the
        cache under this address's key, where something downstream reads them.
        """
        answers = self._confirmed()
        answers["0x3b3b57de"] = "0x" + _word(STRANGER[2:])
        look, chain = self._lookup(answers)
        look.look_up(VITALIK)
        assert not any(data.startswith("0x59d1d43c") for _, data in chain.calls)

    def test_the_refusal_says_whose_records_they_would_be(self) -> None:
        answers = self._confirmed()
        answers["0x3b3b57de"] = "0x" + _word(STRANGER[2:])
        look, _ = self._lookup(answers)
        notes = " ".join(look.look_up(VITALIK).notes)
        assert "about somebody else" in notes

    def test_no_reverse_record_is_not_a_verdict(self) -> None:
        # Most addresses have none, and saying so plainly is the whole content.
        look, _ = self._lookup({"0x0178b8bf": "0x" + _word("0")})
        found = look.look_up(STRANGER)
        assert found.leads == []
        assert "says nothing about who controls it" in " ".join(found.notes)


class TestAZeroResolver:
    def test_it_is_reported_as_absent_not_as_an_address(self) -> None:
        look = EnsLookup(FakeChain({"0x0178b8bf": "0x" + _word("0")}), ETHEREUM)
        assert look.resolver_for(reverse_node(VITALIK)) == ""

    def test_nothing_is_called_against_it(self) -> None:
        """Otherwise empty return data decodes to an empty string, and "no
        resolver" becomes "no Twitter handle" --- a different, unfalsifiable
        claim."""
        chain = FakeChain({"0x0178b8bf": "0x" + _word("0")})
        EnsLookup(chain, ETHEREUM).look_up(VITALIK)
        assert all(to == ENS_REGISTRY for to, _ in chain.calls)

    def test_a_name_with_no_resolver_reports_unchecked_not_unconfirmed(self) -> None:
        # `None`, not `""`. "We did not look" must not read as "it fails".
        look = EnsLookup(FakeChain({"0x0178b8bf": "0x" + _word("0")}), ETHEREUM)
        assert look.address_of("nobody.eth") is None


class TestOnlyKnownKeys:
    def test_it_asks_for_the_keys_it_understands(self) -> None:
        from chainscope.osint.leads import TEXT_KEYS

        chain = FakeChain(
            {
                "0x0178b8bf": "0x" + _word(RESOLVER[2:]),
                "0x691f3431": _string("vitalik.eth"),
                "0x3b3b57de": "0x" + _word(VITALIK[2:]),
                "0x59d1d43c": _string("x"),
            }
        )
        EnsLookup(chain, ETHEREUM).look_up(VITALIK)
        text_calls = sum(1 for _, data in chain.calls if data.startswith("0x59d1d43c"))
        assert text_calls == len(TEXT_KEYS)

    def test_the_node_is_the_namehash_of_the_normalised_name(self) -> None:
        assert namehash(normalise_name("VITALIK.eth")) == namehash("vitalik.eth")
