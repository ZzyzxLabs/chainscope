"""Address normalisation.

The most dangerous function in the codebase, because getting it wrong does not
raise --- it silently makes two different addresses compare equal, or one
address look like two. Every clustering result, every set membership test, and
every "did the funds go here" answer depends on it.
"""

import pytest

from chainscope.chains.base import InvalidAddressError
from chainscope.chains.bitcoin import BitcoinAdapter
from chainscope.chains.evm import EvmAdapter, is_checksum_valid, to_checksum
from chainscope.core.chainid import BITCOIN, ETHEREUM

# USDT on Ethereum, in its EIP-55 form.
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
# Bitcoin genesis coinbase output.
GENESIS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
TAPROOT = "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"


class TestEvm:
    @pytest.fixture
    def evm(self):
        return EvmAdapter()

    def test_normalize_lowercases(self, evm):
        # Safe here and only here: EVM addresses are hex, so case carries no
        # information beyond the optional EIP-55 checksum.
        assert evm.normalize(USDT) == USDT.lower()

    def test_case_variants_compare_equal(self, evm):
        a = evm.address(ETHEREUM, USDT)
        b = evm.address(ETHEREUM, USDT.lower())
        assert a == b and hash(a) == hash(b)

    def test_same_address_different_chains_are_not_equal(self, evm):
        from chainscope.core.chainid import BSC

        assert evm.address(ETHEREUM, USDT) != evm.address(BSC, USDT)

    def test_checksum_round_trip(self):
        assert to_checksum(USDT.lower()) == USDT

    def test_checksum_detects_a_single_flipped_case(self):
        corrupted = USDT[:-2] + "C7"
        assert is_checksum_valid(USDT)
        assert not is_checksum_valid(corrupted)

    def test_caseless_forms_carry_no_checksum(self):
        # All-lower and all-upper are valid addresses that simply omit the
        # checksum; rejecting them would break most real-world input.
        assert is_checksum_valid(USDT.lower())
        assert is_checksum_valid("0x" + USDT[2:].upper())

    @pytest.mark.parametrize(
        "bad",
        [
            "0xdAC17F958D2ee523a2206206994597C13D831ec",  # too short
            "0xdAC17F958D2ee523a2206206994597C13D831ec77",  # too long
            "dAC17F958D2ee523a2206206994597C13D831ec7",  # no 0x
            "0xZZC17F958D2ee523a2206206994597C13D831ec7",  # not hex
            "",
        ],
    )
    def test_rejects_malformed(self, evm, bad):
        assert not evm.is_valid(bad)
        with pytest.raises(InvalidAddressError):
            evm.address(ETHEREUM, bad)

    def test_display_uses_checksum(self, evm):
        assert evm.display(evm.address(ETHEREUM, USDT.lower())) == USDT


class TestBitcoin:
    @pytest.fixture
    def btc(self):
        return BitcoinAdapter()

    def test_base58_case_is_preserved(self, btc):
        """The single most important assertion in this file.

        Lowercasing a base58 address yields either an invalid string or --- far
        worse --- a different valid address. Nothing downstream can detect that.
        """
        assert btc.normalize(GENESIS) == GENESIS
        assert btc.normalize(GENESIS) != GENESIS.lower()

    def test_base58_case_variants_are_different_addresses(self, btc):
        a = btc.address(BITCOIN, GENESIS)
        b = btc.address(BITCOIN, "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divfna")
        assert a != b

    def test_bech32_is_lowercased(self, btc):
        # bech32 is specified as lowercase, so the uppercase transport form
        # normalises down safely.
        assert btc.normalize(BECH32.upper()) == BECH32

    def test_bech32_case_variants_compare_equal(self, btc):
        assert btc.address(BITCOIN, BECH32) == btc.address(BITCOIN, BECH32.upper())

    @pytest.mark.parametrize(
        ("address", "kind"),
        [
            (GENESIS, "p2pkh"),
            ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "p2sh"),
            (BECH32, "p2wpkh"),
            (TAPROOT, "p2tr"),
        ],
    )
    def test_address_kinds(self, btc, address, kind):
        assert btc.address_kind(address) == kind

    @pytest.mark.parametrize(
        "bad",
        [
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfN0",  # 0 is not in the base58 alphabet
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNI",  # nor is I
            "bc1bar0srrr7xfkvy5l643lydnw9re59gtzz",  # b is not in bech32
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # wrong chain entirely
            "",
        ],
    )
    def test_rejects_malformed(self, btc, bad):
        assert not btc.is_valid(bad)

    def test_txid_has_no_0x_prefix(self, btc):
        txid = "e707b322e169ed719843175efab3fa517fe183de72b5ede11a22b53862568a3f"
        assert btc.is_valid_tx(txid)
        assert not btc.is_valid_tx("0x" + txid)


class TestCrossChain:
    def test_an_evm_address_is_not_a_bitcoin_address(self):
        assert not BitcoinAdapter().is_valid(USDT)

    def test_a_bitcoin_address_is_not_an_evm_address(self):
        assert not EvmAdapter().is_valid(GENESIS)
