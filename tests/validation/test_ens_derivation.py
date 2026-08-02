"""ENS namehash against EIP-137's published vectors, and what a name is worth.

The derivation half has real ground truth: EIP-137 publishes namehash values,
so this is byte-exact or it is wrong. That is the same standard applied to the
CREATE derivation, and it is the strongest kind of check available.

The attribution half is about restraint. A reverse record is the most readable
signal on Ethereum and the most easily over-read, and the tests below pin the
distinctions that keep it honest: forward records are claims by strangers,
reverse records are self-declarations, and neither is an identity.
"""

from __future__ import annotations

import pytest

pytest.importorskip("eth_utils", reason="needs chainscope[evm]")

from chainscope.attribution.ens import (
    EnsRecord,
    forward_only_attribution,
    namehash,
    normalise_name,
    resolve_attribution,
    reverse_node,
)
from chainscope.core.attribution import Confidence

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


class TestAgainstTheStandardsVectors:
    """The three values EIP-137 publishes."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("", "0x" + "00" * 32),
            ("eth", "0x93cdeb708b7545dc668eb9280176169d1c33cfd8ed6f04690a0bcc88a93fc4ae"),
            (
                "foo.eth",
                "0xde9b09fd7c5f901e23a3f19fecc54828e9c848539801e86591bd9801b019f84f",
            ),
        ],
    )
    def test_published_vectors(self, name, expected):
        assert "0x" + namehash(name).hex() == expected

    def test_the_empty_name_is_the_root_node(self):
        assert namehash("") == b"\x00" * 32

    def test_it_is_recursive_not_a_flat_hash(self):
        """The hash of a name is its parent's hash concatenated with its
        leftmost label's, which is why a flat keccak of the whole string gives
        a different and wrong answer."""
        from eth_utils import keccak

        assert namehash("foo.eth") != keccak(b"foo.eth")

    def test_a_deeper_name_differs_from_its_parent(self):
        assert namehash("a.foo.eth") != namehash("foo.eth")

    def test_case_does_not_change_the_node(self):
        assert namehash("FOO.ETH") == namehash("foo.eth")

    def test_a_trailing_dot_is_the_same_name(self):
        assert namehash("foo.eth.") == namehash("foo.eth")


class TestReverseNodes:
    def test_the_reverse_node_uses_the_lowercased_address(self):
        assert reverse_node(VITALIK) == reverse_node(VITALIK.lower())

    def test_it_is_under_addr_reverse(self):
        expected = namehash(f"{VITALIK[2:].lower()}.addr.reverse")
        assert reverse_node(VITALIK) == expected

    @pytest.mark.parametrize("bad", ["0xdeadbeef", "", "not an address"])
    def test_a_malformed_address_is_refused(self, bad):
        with pytest.raises(ValueError, match="20-byte"):
            reverse_node(bad)


class TestNormalisationIsHonestAboutItsLimits:
    def test_it_lowercases_and_strips(self):
        assert normalise_name("  Vitalik.ETH.  ") == "vitalik.eth"

    def test_it_does_not_claim_to_handle_confusables(self):
        """The characters UTS-46 exists to normalise are the entire mechanism
        behind ENS impersonation. Two names that render identically stay
        different here, and nothing in this module compares names for
        equality --- only addresses, which have no such ambiguity."""
        latin = "vitalik.eth"
        # Written as an escape rather than pasted: the literal character is
        # invisible in a diff, which is the property that makes it useful for
        # impersonation and useless in source.
        cyrillic = latin.replace("a", "\u0430")  # CYRILLIC SMALL LETTER A
        assert normalise_name(latin) != normalise_name(cyrillic)
        assert namehash(latin) != namehash(cyrillic)


class TestWhatAReverseRecordIsWorth:
    def test_a_forward_confirmed_record_is_medium(self):
        """It establishes self-declaration and nothing more."""
        claim = resolve_attribution(
            EnsRecord(address=VITALIK, name="vitalik.eth", forward_address=VITALIK)
        )
        assert claim is not None
        assert claim.confidence is Confidence.MEDIUM
        assert "self-declaration" in claim.rationale

    def test_it_is_never_high(self):
        """A name is not an identity, and the resemblance between uniswap.eth
        and Uniswap is what an impersonator is counting on."""
        claim = resolve_attribution(
            EnsRecord(address=VITALIK, name="uniswap.eth", forward_address=VITALIK)
        )
        assert claim is not None
        assert claim.confidence <= Confidence.MEDIUM
        assert "impersonator" in claim.rationale

    def test_an_unconfirmed_record_is_low_and_says_why(self):
        other = "0x" + "b" * 40
        claim = resolve_attribution(
            EnsRecord(address=VITALIK, name="somebody.eth", forward_address=other)
        )
        assert claim is not None
        assert claim.confidence is Confidence.LOW
        assert "one side" in claim.rationale

    def test_an_unchecked_record_produces_no_claim_at_all(self):
        """ "We found a name but did not verify it" is not a weaker version of
        "this address is called X". Emitting it as a low-confidence attribution
        invites it to be read as the first one."""
        record = EnsRecord(address=VITALIK, name="vitalik.eth")
        assert not record.was_checked
        assert resolve_attribution(record) is None

    def test_no_name_produces_no_claim(self):
        assert resolve_attribution(EnsRecord(address=VITALIK)) is None

    def test_confirmation_compares_addresses_case_insensitively(self):
        record = EnsRecord(address=VITALIK, name="vitalik.eth", forward_address=VITALIK.lower())
        assert record.is_confirmed


class TestForwardRecordsAreClaimsByStrangers:
    def test_a_forward_record_alone_is_low(self):
        claim = forward_only_attribution("binance-hot-wallet.eth", VITALIK)
        assert claim.confidence is Confidence.LOW

    def test_the_rationale_names_the_asymmetry(self):
        """Anybody can point a name at any address. The address never agreed."""
        claim = forward_only_attribution("binance-hot-wallet.eth", VITALIK)
        assert "not by this address" in claim.rationale
        assert "reverse record" in claim.rationale

    def test_the_label_attributes_the_claim_to_the_name_owner(self):
        """So a reader cannot mistake it for something the address said."""
        claim = forward_only_attribution("treasury.eth", VITALIK)
        assert "by its owner" in claim.label

    def test_it_carries_a_source_like_everything_else(self):
        assert forward_only_attribution("x.eth", VITALIK).source
