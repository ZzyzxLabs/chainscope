"""Five silent-wrong-answer bugs found by reviewing the whole repository.

Grouped here because four of them are one mistake: **the EVM habit of
comparing addresses case-insensitively, applied where case carries value.**
Hex is a checksum and folds; base58 does not. The fifth is a stated threat
model that nothing enforced.
"""

from __future__ import annotations

import json

import pytest

from chainscope.analysis.bytecode import compare
from chainscope.analysis.memo import Memo, authored_by
from chainscope.analysis.revenue import Distribution
from chainscope.attribution.sources.ofac import _key
from chainscope.case.bundle import Bundle, BundleError

BTC = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
SOL = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


class TestTwoNonContractsAreNotTheSameContract:
    def test_empty_against_empty_is_not_identical(self) -> None:
        """The rule was written on the *second* return and jumped over.

        `same_code=bool(code_left) and ...` says empty is not a match with
        empty; an early return on raw equality reached the answer first, so two
        addresses that are not contracts came back as running the same thing.
        """
        result = compare("0x", "0x")
        assert not result.identical
        assert not result.same_code

    @pytest.mark.parametrize("other", ["0x", "0x6080"])
    def test_empty_against_anything_is_not_a_match(self, other: str) -> None:
        assert not compare("0x", other).same_code
        assert not compare(other, "0x").same_code

    def test_real_identical_code_still_matches(self) -> None:
        assert compare("0x6080604052", "0x6080604052").identical


class TestARevenueShareIsFoundOnChecksummedAddresses:
    def test_a_checksummed_key_resolves(self) -> None:
        """It lowercased the query and not the keys.

        Every EVM provider returns checksummed addresses, so a distribution
        built from real data matched nothing and reported every recipient as
        0 bps --- which looks exactly like an address that takes no cut.
        """
        d = Distribution(tx="0x1", payouts={"0xAbCdEf": 100})
        assert d.share_bps("0xAbCdEf") == 10_000

    def test_either_spelling_of_the_query_works(self) -> None:
        d = Distribution(tx="0x1", payouts={"0xAbCdEf": 100})
        assert d.share_bps("0xabcdef") == d.share_bps("0xABCDEF") == 10_000

    def test_two_spellings_of_one_address_are_one_recipient(self) -> None:
        # Adding them is the only answer that keeps `total` and the shares
        # consistent with each other.
        d = Distribution(tx="0x1", payouts={"0xAb": 60, "0xaB": 40})
        assert d.share_bps("0xab") == 10_000

    def test_a_share_of_a_split_is_still_proportional(self) -> None:
        d = Distribution(tx="0x1", payouts={"0xAAA": 250, "0xBBB": 750})
        assert d.share_bps("0xaaa") == 2_500


class TestBase58AddressesKeepTheirCase:
    def test_a_solana_signer_is_compared_exactly(self) -> None:
        """`7xKX` and `7xkx` are different accounts.

        Lowercasing before comparing invents matches between unrelated
        accounts, and because base58 excludes some characters it can fold two
        real addresses onto one key.
        """
        mine = Memo(tx="0x1", signer=SOL, raw="hello")
        theirs = Memo(tx="0x2", signer=SOL.lower(), raw="not mine")
        feed = authored_by([mine, theirs], SOL)
        assert feed.own == [mine]
        assert feed.injected == [theirs]

    def test_the_address_is_not_rewritten(self) -> None:
        feed = authored_by([], SOL)
        assert feed.address == SOL

    def test_ofac_folds_hex_and_not_base58(self) -> None:
        # A sanctions list is the worst place for either error: one is a false
        # positive against an innocent address, the other is a listed address
        # that screens clean.
        evm = "0x" + "A" * 40
        assert _key(evm) == _key(evm.lower())
        assert _key(BTC) != _key(BTC.lower())

    def test_a_hex_string_that_is_not_an_address_is_left_alone(self) -> None:
        # Length is the discriminator; a 66-char hash is not an EVM address.
        long_hex = "0x" + "A" * 64
        assert _key(long_hex) == long_hex


class TestABundleMayOnlyNameFilesInsideItself:
    def _bundle(self, tmp_path, filename: str) -> Bundle:
        root = tmp_path / "case.bundle"
        (root / "results").mkdir(parents=True)
        (root / "manifest.json").write_text(
            json.dumps(
                {"manifest_version": 1, "results": [{"file": filename, "analyzer": "x"}]}
            )
        )
        (root / "results" / "ok.json").write_text('{"findings": []}')
        return Bundle.open(root)

    def test_an_ordinary_entry_reads(self, tmp_path) -> None:
        assert self._bundle(tmp_path, "ok.json").read_result(0) == {"findings": []}

    @pytest.mark.parametrize(
        "filename",
        [
            pytest.param("../../../etc/passwd", id="traversal"),
            pytest.param("/etc/passwd", id="absolute"),
            pytest.param("../manifest.json", id="one level up"),
        ],
    )
    def test_an_escaping_entry_is_refused(self, tmp_path, filename: str) -> None:
        """`pathlib` discards the left side of a join with an absolute path.

        So an entry reading `/etc/passwd` read `/etc/passwd`. The module's own
        docstring says a bundle is untrusted input produced by somebody else ---
        a stated threat model that nothing enforced.
        """
        bundle = self._bundle(tmp_path, filename)
        with pytest.raises(BundleError, match="outside the bundle"):
            bundle.read_result(0)
