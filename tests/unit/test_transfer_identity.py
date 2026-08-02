"""What makes two rows the same transfer.

`asset` was missing from the uniqueness key, so two transfers of equal raw
amounts of *different* tokens, in one transaction between one pair of
addresses, collided --- and `INSERT OR IGNORE` dropped the second with no
error. A DEX routing through two pools, or an airdrop sending equal units of
two tokens, produces exactly that shape.

The loss was invisible, which is what makes it worth a file of its own: the
store simply held fewer rows than it was handed, and every total computed from
it was quietly low.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.base import Query
from chainscope.store.sqlite import SqliteStore

TX = TxRef(ETHEREUM, "0x" + "1" * 64)
A = Address(ETHEREUM, "0xa", "0xa")
B = Address(ETHEREUM, "0xb", "0xb")


def transfer(*, asset=None, raw=1000, index=0, symbol="TKN", kind=None):
    return Transfer(
        chain=ETHEREUM,
        tx=TX,
        sender=A,
        recipient=B,
        amount=Amount(raw, 18, symbol),
        kind=kind or (TransferKind.TOKEN if asset else TransferKind.NATIVE),
        block=1,
        index=index,
        asset=Address(ETHEREUM, asset, asset) if asset else None,
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.db")
    yield s
    s.close()


def count(store):
    return len(list(store.transfers(Query(chain=ETHEREUM))))


class TestTwoAssetsAreTwoTransfers:
    def test_equal_amounts_of_different_tokens_both_survive(self, store):
        store.put_transfers(
            [transfer(asset="0xusdc", symbol="USDC"), transfer(asset="0xwbtc", symbol="WBTC")],
            source="t",
        )
        assert count(store) == 2

    def test_the_symbols_are_both_readable_afterwards(self, store):
        store.put_transfers(
            [transfer(asset="0xusdc", symbol="USDC"), transfer(asset="0xwbtc", symbol="WBTC")],
            source="t",
        )
        assert {t.amount.symbol for t in store.transfers(Query(chain=ETHEREUM))} == {
            "USDC",
            "WBTC",
        }

    def test_a_token_and_a_native_transfer_of_the_same_size_both_survive(self, store):
        store.put_transfers([transfer(asset="0xusdc"), transfer()], source="t")
        assert count(store) == 2


class TestDeduplicationStillWorks:
    """The fix must not trade a silent loss for a silent duplicate."""

    def test_the_same_token_transfer_twice_is_one_row(self, store):
        store.put_transfers([transfer(asset="0xusdc")], source="t")
        store.put_transfers([transfer(asset="0xusdc")], source="t")
        assert count(store) == 1

    def test_the_same_native_transfer_twice_is_one_row(self, store):
        """NULLs are distinct in a SQLite unique index, so without COALESCE a
        native transfer would stop deduplicating entirely."""
        store.put_transfers([transfer()], source="t")
        store.put_transfers([transfer()], source="t")
        assert count(store) == 1

    def test_within_one_batch_too(self, store):
        store.put_transfers([transfer(), transfer()], source="t")
        assert count(store) == 1

    def test_a_different_log_index_is_a_different_transfer(self, store):
        store.put_transfers([transfer(index=0), transfer(index=1)], source="t")
        assert count(store) == 2

    def test_a_different_amount_is_a_different_transfer(self, store):
        store.put_transfers([transfer(raw=1000), transfer(raw=1001)], source="t")
        assert count(store) == 2
