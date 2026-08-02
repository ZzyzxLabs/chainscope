"""The store and the chain adapters must not disagree about an address.

`ChainAdapter.normalize` is where the case rule lives, and its docstring says
what an error there costs: it does not raise, it silently makes two addresses
look identical or one address look like two.

The rule was written once and then bypassed --- the store called `.lower()` on
every address it touched. On Solana, Sui and Bitcoin that wrote base58 into
`transfers` and lowercase into `expanded`, and the two never matched again.
Measured before the fix: a transfer query by a Solana address returned **zero**
rows, and an address that had been expanded stayed on the frontier forever.

This file exists so the two cannot drift apart again.
"""

from __future__ import annotations

import pytest

from chainscope.chains import adapter_for, address_key
from chainscope.core.chainid import ChainId
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.base import Query
from chainscope.store.sqlite import SqliteStore

CHAINS = {
    "eip155": ("eip155:1", "0xAbCdEf" + "0" * 34),
    "solana": (
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
    ),
    "bip122": (
        "bip122:000000000019d6689c085ae165831e93",
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
    ),
}


@pytest.mark.parametrize("namespace", sorted(CHAINS))
class TestTheStoreUsesTheAdaptersRule:
    def test_address_key_delegates_to_the_adapter(self, namespace: str) -> None:
        caip, address = CHAINS[namespace]
        adapter = adapter_for(namespace)
        assert adapter is not None, f"no adapter for {namespace}"
        assert address_key(ChainId.parse(caip), address) == adapter.normalize(address)

    def test_a_transfer_is_found_by_the_address_that_wrote_it(self, namespace: str) -> None:
        caip, address = CHAINS[namespace]
        chain = ChainId.parse(caip)
        store = SqliteStore(":memory:")
        try:
            here = Address(chain, address, address)
            store.put_transfers(
                [
                    Transfer(
                        chain=chain,
                        tx=TxRef(chain, "0x1"),
                        sender=here,
                        recipient=here,
                        amount=Amount(1, 9, "X"),
                        kind=TransferKind.NATIVE,
                        block=1,
                    )
                ]
            )
            assert store.count(Query(chain=chain, address=address)) == 1
        finally:
            store.close()

    def test_an_expanded_address_leaves_the_frontier(self, namespace: str) -> None:
        """The frontier is documented as the honest boundary of a case.

        Comparing a lowercased `expanded` row against a base58 `transfers` row
        never matched, so every non-EVM address stayed frontier forever --- an
        overstated boundary is safer than an understated one, and still wrong.
        """
        caip, address = CHAINS[namespace]
        chain = ChainId.parse(caip)
        store = SqliteStore(":memory:")
        try:
            here = Address(chain, address, address)
            store.put_transfers(
                [
                    Transfer(
                        chain=chain,
                        tx=TxRef(chain, "0x1"),
                        sender=here,
                        recipient=here,
                        amount=Amount(1, 9, "X"),
                        kind=TransferKind.NATIVE,
                        block=1,
                    )
                ]
            )
            assert store.frontier(chain), "should start on the frontier"
            store.mark_expanded(address, chain)
            assert store.frontier(chain) == []
        finally:
            store.close()


class TestTheRulesThemselves:
    def test_evm_folds_case(self) -> None:
        chain = ChainId.parse("eip155:1")
        address = "0xAbCdEf" + "0" * 34
        assert address_key(chain, address) == address_key(chain, address.lower())

    @pytest.mark.parametrize("namespace", ["solana", "bip122"])
    def test_others_do_not(self, namespace: str) -> None:
        # Both spellings can be valid and belong to different people.
        caip, address = CHAINS[namespace]
        chain = ChainId.parse(caip)
        assert address_key(chain, address) != address_key(chain, address.lower())

    def test_an_unknown_namespace_leaves_the_address_alone(self) -> None:
        """Never `.lower()`.

        An unknown namespace is by definition not EVM, so lowercasing could only
        destroy information --- and the failure that causes (one address looking
        like two) is a miss, not a false match between two people's addresses.
        """
        assert adapter_for("nosuchchain") is None
        assert address_key("nosuchchain:1", "MixedCase") == "MixedCase"

    def test_no_chain_at_all_leaves_it_alone_too(self) -> None:
        assert address_key(None, "MixedCase") == "MixedCase"


class TestWriteAndReadAgreeByConstruction:
    def test_an_unnormalised_key_is_normalised_on_write(self) -> None:
        """`put_transfers` used to store `Address.key` as handed to it.

        The adapters set it correctly; anything hand-built may not, and a row
        written with an unnormalised key is a row no query finds again.
        """
        chain = ChainId.parse("eip155:1")
        checksummed = "0xAbCdEf" + "0" * 34
        # Deliberately wrong: `key` should be lowercase for EVM.
        hand_built = Address(chain, checksummed, checksummed)
        store = SqliteStore(":memory:")
        try:
            store.put_transfers(
                [
                    Transfer(
                        chain=chain,
                        tx=TxRef(chain, "0x1"),
                        sender=hand_built,
                        recipient=hand_built,
                        amount=Amount(1, 18, "ETH"),
                        kind=TransferKind.NATIVE,
                        block=1,
                    )
                ]
            )
            assert store.count(Query(chain=chain, address=checksummed)) == 1
            assert store.count(Query(chain=chain, address=checksummed.lower())) == 1
        finally:
            store.close()
