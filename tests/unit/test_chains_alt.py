"""Solana and Tron address handling.

Both chains punish the same mistake in different ways. Solana: lowercasing a
base58 address may yield a *different valid address*. Tron: an address appears
in one form in explorers and another inside event logs, and comparing across
them as strings finds nothing — which reads as "the funds did not go there".
"""

import pytest

from chainscope.chains.solana import SolanaAdapter
from chainscope.chains.tron import TronAdapter, base58_to_hex, hex_to_base58
from chainscope.core.chainid import SOLANA, TRON

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_HEX = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
USDT_EVM = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"


class TestSolana:
    @pytest.fixture
    def sol(self):
        return SolanaAdapter()

    @pytest.mark.parametrize("address", [USDC_MINT, WSOL_MINT, TOKEN_PROGRAM])
    def test_accepts_real_addresses(self, sol, address):
        assert sol.is_valid(address)

    def test_case_is_preserved(self, sol):
        assert sol.normalize(USDC_MINT) == USDC_MINT

    def test_lowercasing_produces_an_invalid_address(self, sol):
        """And if it did not, it would be someone else's address."""
        assert not sol.is_valid(USDC_MINT.lower())

    def test_case_variants_are_different_addresses(self, sol):
        a = sol.address(SOLANA, USDC_MINT)
        b = sol.address(SOLANA, WSOL_MINT)
        assert a != b

    @pytest.mark.parametrize(
        "bad",
        [
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1O",  # O is not base58
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt10",  # nor is 0
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # an EVM address
            "short",
            "",
        ],
    )
    def test_rejects_malformed(self, sol, bad):
        assert not sol.is_valid(bad)

    def test_signature_format(self, sol):
        sig = (
            "5j7s6NiJS3JAkvgkoc18WVAsiSaci2pxB2A6ueCJP4tprA2TFg9wSyTLeYouxPBJEMzJ"
            "inENTkpA52YStRW5Dia7"
        )
        assert sol.is_valid_tx(sig)
        assert not sol.is_valid_tx("0x" + sig)  # Solana signatures carry no 0x


class TestTron:
    @pytest.fixture
    def tron(self):
        return TronAdapter()

    def test_accepts_base58(self, tron):
        assert tron.is_valid(USDT_TRC20)

    def test_accepts_both_hex_forms(self, tron):
        assert tron.is_valid(USDT_HEX)
        assert tron.is_valid(USDT_EVM)

    def test_hex_to_base58(self):
        assert hex_to_base58(USDT_HEX) == USDT_TRC20

    def test_base58_to_hex(self):
        assert base58_to_hex(USDT_TRC20) == USDT_HEX

    def test_evm_style_drops_the_41_prefix(self):
        """This is the form that appears inside TRC-20 event log topics."""
        assert base58_to_hex(USDT_TRC20, evm_style=True) == USDT_EVM

    def test_conversion_round_trips(self):
        assert hex_to_base58(base58_to_hex(USDT_TRC20)) == USDT_TRC20

    def test_log_derived_address_compares_equal_to_the_user_facing_one(self, tron):
        """The whole reason this adapter normalises across forms.

        Without it, a TRC-20 transfer to this contract looks like a transfer to
        an unrelated address, and the trace silently loses the destination.
        """
        assert tron.normalize(USDT_EVM) == USDT_TRC20
        assert tron.address(TRON, USDT_EVM) == tron.address(TRON, USDT_TRC20)

    def test_base58_case_is_preserved(self, tron):
        assert tron.normalize(USDT_TRC20) == USDT_TRC20

    @pytest.mark.parametrize(
        "bad",
        [
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6",  # too short
            "XR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # wrong prefix
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # a Solana address
            "",
        ],
    )
    def test_rejects_malformed(self, tron, bad):
        assert not tron.is_valid(bad)

    def test_bad_hex_conversion_raises(self):
        with pytest.raises(ValueError, match="not a Tron hex address"):
            hex_to_base58("deadbeef")

    def test_txid_has_no_prefix(self, tron):
        txid = "e707b322e169ed719843175efab3fa517fe183de72b5ede11a22b53862568a3f"
        assert tron.is_valid_tx(txid)
        assert not tron.is_valid_tx("0x" + txid)


class TestCrossEcosystem:
    def test_addresses_do_not_validate_on_the_wrong_chain(self):
        sol, tron = SolanaAdapter(), TronAdapter()
        assert not sol.is_valid(USDT_TRC20)
        assert not tron.is_valid(USDC_MINT)
