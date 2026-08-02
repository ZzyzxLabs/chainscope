"""Sui address handling and provider.

Everything here guards against code written by analogy with EVM. Sui addresses
are 32 bytes rather than 20, short forms are legal and mean the padded address,
the native asset has nine decimals rather than eighteen, and the balance change
attributed to a sender includes the gas they paid.

Each of those fails silently if unhandled: a truncated address that is still
well-formed, one entity that looks like two, a balance a billion times too
small, and every outbound transfer overstated by its fee.
"""

import pytest

from chainscope.chains.base import InvalidAddressError
from chainscope.chains.sui import (
    SUI_DECIMALS,
    SUI_MAINNET,
    SUI_TYPE,
    SuiAdapter,
    coin_decimals,
    coin_symbol,
    normalize_address,
    normalize_coin_type,
)
from chainscope.core.chainid import ChainId, Ecosystem
from chainscope.core.models import TransferKind
from chainscope.providers.base import Capability, ProviderError
from chainscope.providers.sui import SuiProvider

ALICE = "0x" + "a" * 64
BOB = "0x" + "b" * 64
FRAMEWORK_SHORT = "0x2"
FRAMEWORK_FULL = "0x" + "0" * 63 + "2"

ONE_SUI = 10**9


class FakeClient:
    """Serves canned JSON-RPC results keyed by method."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def rpc(self, url, method, params=None, **kw):
        self.calls.append((method, params))
        value = self.results.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(params)
        return value


def tx(
    digest="0x" + "d" * 64,
    changes=(),
    gas=1_000_000,
    checkpoint=100,
    timestamp_ms=1_700_000_000_000,
):
    return {
        "digest": digest,
        "checkpoint": str(checkpoint),
        "timestampMs": str(timestamp_ms),
        "balanceChanges": list(changes),
        "effects": {
            "gasUsed": {
                "computationCost": str(gas),
                "storageCost": "0",
                "storageRebate": "0",
            }
        },
    }


def change(owner, amount, coin=SUI_TYPE):
    return {"owner": {"AddressOwner": owner}, "coinType": coin, "amount": str(amount)}


def page(items, more=False, cursor=None):
    return {"data": list(items), "hasNextPage": more, "nextCursor": cursor}


def provider(results):
    return SuiProvider(client=FakeClient(results))


class TestAddresses:
    def test_full_addresses_normalise(self):
        assert normalize_address(ALICE.upper()) == ALICE

    def test_short_forms_pad_to_the_same_address(self):
        """0x2 is the framework package and is written that way everywhere.
        Left unpadded it becomes a second entity."""
        assert normalize_address(FRAMEWORK_SHORT) == FRAMEWORK_FULL
        assert normalize_address(FRAMEWORK_FULL) == FRAMEWORK_FULL

    def test_a_normalised_address_is_always_66_characters(self):
        assert len(normalize_address(FRAMEWORK_SHORT)) == 66

    def test_a_twenty_byte_address_is_padded_not_accepted_as_evm(self):
        """An EVM-length address is a valid *short form* on Sui, and must not
        silently pass through as though the 32-byte one."""
        evm = "0x" + "c" * 40
        assert normalize_address(evm) == "0x" + "0" * 24 + "c" * 40

    @pytest.mark.parametrize("bad", ["", "0x", "nothex", "0x" + "a" * 65, "a" * 64])
    def test_malformed_addresses_are_rejected(self, bad):
        with pytest.raises(InvalidAddressError):
            normalize_address(bad)

    def test_the_adapter_agrees(self):
        adapter = SuiAdapter()
        assert adapter.ecosystem is Ecosystem.SUI
        assert adapter.native_decimals == 9
        assert adapter.is_valid(FRAMEWORK_SHORT)
        assert not adapter.is_valid("nonsense")

    def test_the_chain_id_is_valid_caip2(self):
        assert str(SUI_MAINNET) == "sui:mainnet"
        assert ChainId("sui", "testnet").namespace == "sui"

    def test_the_reference_is_not_numeric(self):
        """Anything assuming an integer chain id breaks here."""
        assert SUI_MAINNET.evm_chain_id is None


class TestCoinTypes:
    def test_the_package_is_padded(self):
        assert normalize_coin_type(SUI_TYPE).startswith(FRAMEWORK_FULL)

    def test_two_spellings_of_one_coin_agree(self):
        assert normalize_coin_type("0x2::sui::SUI") == normalize_coin_type(
            f"{FRAMEWORK_FULL}::sui::SUI"
        )

    def test_the_symbol_is_a_display_detail(self):
        assert coin_symbol("0x2::sui::SUI") == "SUI"
        assert coin_symbol("0xdead::fake::USDC") == "USDC"

    def test_two_coins_can_share_a_symbol(self):
        """Which is why the package, not the symbol, is the identity."""
        a = normalize_coin_type("0xaaa::coin::USDC")
        b = normalize_coin_type("0xbbb::coin::USDC")
        assert coin_symbol(a) == coin_symbol(b)
        assert a != b

    def test_decimals_are_known_only_for_the_native_asset(self):
        """Guessing nine for an arbitrary coin is a silent order-of-magnitude
        error, so an unknown coin returns None and the caller must ask."""
        assert coin_decimals(SUI_TYPE) == SUI_DECIMALS
        assert coin_decimals("0xdead::fake::USDC") is None

    def test_a_malformed_coin_type_is_rejected(self):
        with pytest.raises(InvalidAddressError):
            normalize_coin_type("not::a::type")


class TestProviderShape:
    def test_it_declares_address_history(self):
        """The capability EVM needs an explorer for, and Sui answers natively."""
        p = provider({})
        assert p.capabilities.covers(Capability.ADDRESS_HISTORY)

    def test_it_needs_no_key(self):
        from chainscope.providers.base import CostTier

        assert provider({}).cost is CostTier.FREE_PUBLIC

    def test_the_cache_is_scoped_by_chain_not_url(self):
        """Two fullnodes serving mainnet must share cache entries."""
        p = provider({"suix_getBalance": {"totalBalance": "0"}})
        p.get_account(SUI_MAINNET, ALICE)
        assert p.client.calls[0][0] == "suix_getBalance"

    def test_a_bad_direction_is_rejected(self):
        with pytest.raises(ProviderError, match="direction"):
            provider({}).asset_transfers(SUI_MAINNET, ALICE, direction="sideways")


class TestGasCorrection:
    def _one_transfer(self, gas):
        return {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        tx(
                            changes=[
                                change(ALICE, -(ONE_SUI + gas)),
                                change(BOB, ONE_SUI),
                            ],
                            gas=gas,
                        )
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }

    def test_the_transferred_amount_excludes_gas(self):
        """Alice's balance fell by 1 SUI + gas; she transferred 1 SUI.

        Reading the balance change as the amount overstates every outbound
        transfer by its fee -- small per transaction, systematically wrong
        across a sweep.
        """
        p = provider(self._one_transfer(gas=5_000_000))
        (transfer,) = p.asset_transfers(SUI_MAINNET, ALICE, direction="out")
        assert transfer.amount.raw == ONE_SUI

    def test_a_gas_only_transaction_produces_no_transfer(self):
        """After the correction the net movement is zero, which is the truth:
        paying a fee is not sending anyone anything."""
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page([tx(changes=[change(ALICE, -1_000_000)], gas=1_000_000)])
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }
        assert provider(results).asset_transfers(SUI_MAINNET, ALICE, direction="out") == []

    def test_the_storage_rebate_reduces_the_gas(self):
        """Ignoring the rebate overstates gas, which then under-reports the
        transfer it is subtracted from."""
        raw = {
            "digest": "0x" + "d" * 64,
            "checkpoint": "100",
            "timestampMs": "1700000000000",
            "balanceChanges": [change(ALICE, -(ONE_SUI + 3_000_000)), change(BOB, ONE_SUI)],
            "effects": {
                "gasUsed": {
                    "computationCost": "5000000",
                    "storageCost": "0",
                    "storageRebate": "2000000",
                }
            },
        }
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page([raw]) if params[0]["filter"].get("FromAddress") else page([])
            )
        }
        (transfer,) = provider(results).asset_transfers(SUI_MAINNET, ALICE, direction="out")
        assert transfer.amount.raw == ONE_SUI


class TestTransfers:
    def _results(self):
        return {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        tx(
                            changes=[
                                change(ALICE, -(2 * ONE_SUI + 1_000_000)),
                                change(BOB, 2 * ONE_SUI),
                            ]
                        )
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }

    def test_native_decimals_are_nine(self):
        """Eighteen makes a balance look a billion times smaller -- small
        enough to be mistaken for dust and skipped."""
        (transfer,) = provider(self._results()).asset_transfers(
            SUI_MAINNET, ALICE, direction="out"
        )
        assert transfer.amount.decimals == 9
        assert str(transfer.amount) == "2 SUI"

    def test_the_native_asset_is_marked_native(self):
        (transfer,) = provider(self._results()).asset_transfers(
            SUI_MAINNET, ALICE, direction="out"
        )
        assert transfer.kind is TransferKind.NATIVE
        assert transfer.asset is None

    def test_a_non_native_coin_is_marked_token(self):
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        tx(
                            changes=[
                                change(ALICE, -1000, coin="0xdead::coin::USDC"),
                                change(BOB, 1000, coin="0xdead::coin::USDC"),
                            ]
                        )
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }
        (transfer,) = provider(results).asset_transfers(SUI_MAINNET, ALICE, direction="out")
        assert transfer.kind is TransferKind.TOKEN
        assert transfer.amount.symbol == "USDC"

    def test_object_owners_are_not_treated_as_addresses(self):
        """Only AddressOwner is an account. An object id in the graph looks
        like something somebody controls."""
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        {
                            "digest": "0x" + "d" * 64,
                            "checkpoint": "1",
                            "timestampMs": "1700000000000",
                            "balanceChanges": [
                                change(ALICE, -ONE_SUI),
                                {
                                    "owner": {"ObjectOwner": BOB},
                                    "coinType": SUI_TYPE,
                                    "amount": "1",
                                },
                            ],
                            "effects": {"gasUsed": {}},
                        }
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }
        assert provider(results).asset_transfers(SUI_MAINNET, ALICE, direction="out") == []

    def test_addresses_are_normalised_in_the_query(self):
        results = {"suix_queryTransactionBlocks": lambda params: page([])}
        p = provider(results)
        p.asset_transfers(SUI_MAINNET, FRAMEWORK_SHORT, direction="out")
        sent = p.client.calls[0][1][0]["filter"]["FromAddress"]
        assert sent == FRAMEWORK_FULL


class TestPagination:
    def test_has_next_page_drives_the_loop(self):
        """A short page is not evidence of the end."""
        pages = [
            page([tx(digest="0x" + "1" * 64)], more=True, cursor="c1"),
            page([tx(digest="0x" + "2" * 64)], more=False),
        ]
        calls = {"n": 0}

        def serve(params):
            if not params[0]["filter"].get("FromAddress"):
                return page([])
            result = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return result

        p = provider({"suix_queryTransactionBlocks": serve})
        p.asset_transfers(SUI_MAINNET, ALICE, direction="out", limit=100)
        assert calls["n"] == 2

    def test_a_missing_cursor_does_not_loop_forever(self):
        """hasNextPage true with no cursor would re-request the same page."""
        results = {
            "suix_queryTransactionBlocks": lambda params: page(
                [tx()] if params[0]["filter"].get("FromAddress") else [],
                more=True,
                cursor=None,
            )
        }
        p = provider(results)
        p.asset_transfers(SUI_MAINNET, ALICE, direction="out", limit=100)
        assert len(p.client.calls) <= 2

    def test_a_malformed_page_is_rejected(self):
        with pytest.raises(ProviderError, match="unexpected response shape"):
            provider({"suix_queryTransactionBlocks": "not a dict"}).asset_transfers(
                SUI_MAINNET, ALICE, direction="out"
            )


class TestAccount:
    def test_balance_uses_nine_decimals(self):
        p = provider({"suix_getBalance": {"totalBalance": str(3 * ONE_SUI)}})
        account = p.get_account(SUI_MAINNET, ALICE)
        assert account.balance.raw == 3 * ONE_SUI
        assert str(account.balance) == "3 SUI"

    def test_there_is_no_nonce(self):
        """Sui has none, so completeness cannot be checked that way. None is
        honest; zero would read as "no transactions"."""
        p = provider({"suix_getBalance": {"totalBalance": "0"}})
        assert p.get_account(SUI_MAINNET, ALICE).tx_count is None

    def test_all_balances_report_unknown_decimals_as_base_units(self):
        p = provider(
            {
                "suix_getAllBalances": [
                    {"coinType": SUI_TYPE, "totalBalance": str(ONE_SUI)},
                    {"coinType": "0xdead::coin::USDC", "totalBalance": "5000000"},
                ]
            }
        )
        native, token = p.balances(ALICE)
        assert native.decimals == 9
        assert token.decimals == 0  # deliberately not guessed


class TestHistory:
    def test_a_transaction_has_no_single_recipient(self):
        """Not a gap: a Sui transaction can touch many addresses, so the detail
        lives in `transfers` rather than in a top-level counterparty."""
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        tx(
                            changes=[
                                change(ALICE, -(2 * ONE_SUI + 1_000_000)),
                                change(BOB, ONE_SUI),
                                change("0x" + "c" * 64, ONE_SUI),
                            ]
                        )
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }
        (transaction,) = provider(results).address_history(SUI_MAINNET, ALICE)
        assert transaction.recipient is None
        assert len(transaction.transfers) == 2
        assert transaction.value.raw == 2 * ONE_SUI

    def test_a_block_range_filters_client_side(self):
        results = {
            "suix_queryTransactionBlocks": lambda params: (
                page(
                    [
                        tx(
                            digest="0x" + "1" * 64,
                            checkpoint=50,
                            changes=[change(ALICE, -ONE_SUI), change(BOB, ONE_SUI)],
                        ),
                        tx(
                            digest="0x" + "2" * 64,
                            checkpoint=500,
                            changes=[change(ALICE, -ONE_SUI), change(BOB, ONE_SUI)],
                        ),
                    ]
                )
                if params[0]["filter"].get("FromAddress")
                else page([])
            )
        }
        got = provider(results).address_history(SUI_MAINNET, ALICE, start_block=100)
        assert [t.block for t in got] == [500]
