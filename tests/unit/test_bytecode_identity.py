"""Same contract, different address --- and the metadata that hides it.

The scam-factory question. Two deployments of identical source usually have
*different* bytecode, because Solidity appends a CBOR metadata blob carrying
the source hash and compiler version. Touch a comment, compile elsewhere, bump
solc by a patch release, and that tail changes while every instruction before
it is byte-identical.

The tail is self-describing: its last two bytes are its own length.
"""

from __future__ import annotations

import pytest

from chainscope.analysis.bytecode import (
    MAX_METADATA,
    compare,
    group_by_code,
    strip_metadata,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM

CODE = "60806040523480156100105760006000fd5b50"


def deployed(code: str, metadata: str) -> str:
    """A runtime blob with a well-formed CBOR tail of the right declared size."""
    blob = bytes.fromhex(metadata)
    return "0x" + code + blob.hex() + len(blob).to_bytes(2, "big").hex()


class TestStripping:
    def test_it_removes_a_well_formed_tail(self):
        assert strip_metadata(deployed(CODE, "aa" * 50)) == bytes.fromhex(CODE)

    def test_the_length_header_goes_too(self):
        """L + 2, not L. Leaving the header behind keeps two bytes that differ
        between builds and makes every comparison fail."""
        raw = deployed(CODE, "aa" * 50)
        assert len(strip_metadata(raw)) == len(bytes.fromhex(CODE))

    def test_an_absurd_length_is_ignored(self):
        """A wrong cut is worse than no cut: it removes real instructions and
        can make two unrelated contracts compare equal."""
        raw = "0x" + CODE + "ffff"
        assert strip_metadata(raw) == bytes.fromhex(CODE + "ffff")

    def test_a_length_longer_than_the_code_is_ignored(self):
        raw = "0x" + "60" * 4 + "0100"
        assert strip_metadata(raw) == bytes.fromhex("60" * 4 + "0100")

    def test_a_zero_length_is_ignored(self):
        raw = "0x" + CODE + "0000"
        assert strip_metadata(raw) == bytes.fromhex(CODE + "0000")

    def test_the_cap_is_what_the_module_documents(self):
        assert MAX_METADATA == 256

    @pytest.mark.parametrize("value", ["0x", "0x00", ""])
    def test_tiny_inputs_survive(self, value):
        strip_metadata(value)

    def test_odd_hex_is_refused(self):
        with pytest.raises(ValueError, match="odd number"):
            strip_metadata("0xabc")

    def test_non_hex_is_refused(self):
        with pytest.raises(ValueError, match="not hex"):
            strip_metadata("0xzz")


class TestComparing:
    def test_identical_deployments_are_reported_as_identical(self):
        """A stronger statement than "their code sections match", and worth
        keeping separate."""
        one = deployed(CODE, "aa" * 40)
        result = compare(one, one)
        assert result.identical
        assert result.verdict == "identical"

    def test_same_source_different_build_matches_on_code(self):
        left = deployed(CODE, "aa" * 40)
        right = deployed(CODE, "bb" * 44)
        result = compare(left, right)
        assert not result.identical
        assert result.same_code
        assert result.stripped_bytes == (42, 46)

    def test_different_instructions_do_not_match(self):
        left = deployed(CODE, "aa" * 40)
        right = deployed("60806040" + "ff" * 8, "aa" * 40)
        assert not compare(left, right).same_code

    def test_two_empty_contracts_are_not_a_family(self):
        """Otherwise every address that is not a contract is reported as
        running the same thing."""
        assert not compare("0x", "0x").same_code or compare("0x", "0x").identical
        assert not compare("0x", "0x0000").same_code

    def test_the_summary_explains_a_metadata_only_difference(self):
        result = compare(deployed(CODE, "aa" * 40), deployed(CODE, "bb" * 40))
        assert "compiled separately" in result.summary()


class TestTheClaim:
    def test_a_match_is_medium_at_most(self):
        result = compare(deployed(CODE, "aa" * 40), deployed(CODE, "bb" * 40))
        claim = result.attribution("0x" + "a" * 40, ETHEREUM)
        assert claim is not None
        assert claim.confidence <= Confidence.MEDIUM

    def test_it_says_shared_code_is_not_shared_control(self):
        """A kit sold to twenty operators produces twenty identical
        deployments run by twenty different people."""
        claim = compare(deployed(CODE, "aa" * 40), deployed(CODE, "bb" * 40)).attribution("0xa")
        assert "none about who deployed" in claim.rationale
        assert "twenty different people" in claim.rationale

    def test_no_match_makes_no_claim(self):
        result = compare(deployed(CODE, "aa" * 40), deployed("60ff", "aa" * 40))
        assert result.attribution("0xa") is None

    def test_the_claim_carries_a_source(self):
        claim = compare(deployed(CODE, "aa" * 40), deployed(CODE, "bb" * 40)).attribution("0xa")
        assert claim.source


class TestGrouping:
    def test_a_family_survives_recompilation(self):
        """The scam-factory question: given fifty addresses, which are the same
        contract?"""
        families = group_by_code(
            {
                "0xa": deployed(CODE, "aa" * 40),
                "0xb": deployed(CODE, "bb" * 44),
                "0xc": deployed(CODE, "cc" * 30),
                "0xd": deployed("60ff60ff", "aa" * 40),
            }
        )
        sizes = sorted(len(m) for m in families.values())
        assert sizes == [1, 3]

    def test_members_are_sorted_for_a_stable_report(self):
        families = group_by_code(
            {"0xc": deployed(CODE, "aa" * 40), "0xa": deployed(CODE, "bb" * 40)}
        )
        assert next(iter(families.values())) == ["0xa", "0xc"]

    def test_addresses_with_no_code_are_left_out(self):
        """An EOA or a self-destructed contract. Grouping them under the empty
        key would report every non-contract as one family."""
        families = group_by_code({"0xeoa": "0x", "0xa": deployed(CODE, "aa" * 40)})
        assert all("0xeoa" not in members for members in families.values())
