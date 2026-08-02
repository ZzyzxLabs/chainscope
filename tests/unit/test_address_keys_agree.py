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

from typing import ClassVar

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


class TestTheCaseFoldDoesNotSpread:
    """A ratchet, and it is debt rather than a pass.

    `.lower()` on an address is correct on EVM and wrong on Solana, Sui and
    Bitcoin. It was found in the store, the local label source, the OFAC
    source, the revenue analyzer, the graph renderer, the memo analyzer and the
    guide plugin authors copy from --- all fixed. A grep then found **27** sites
    in total.

    About ten are correct: an Etherscan or Blockscout provider only ever serves
    EVM chains, ENS is Ethereum-only, and TRON's hex leg is hex. The rest sit in
    chain-agnostic code --- taint, temporal, mixer, the flow renderer, the
    resolver, watch --- and are wrong on three of the five supported chains.

    They are counted here rather than quietly left. The count may **shrink and
    never grow**: a new site fails this test, so the fold cannot spread further
    while the existing ones are worked through. Saying "27 known, none new" is
    a true statement about a bounded problem; a green test with no list would
    be a false one.
    """

    import pathlib

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    #: Correct: these files only ever handle hex addresses.
    EVM_ONLY: ClassVar[set[str]] = {
        "chains/evm.py",
        "chains/bitcoin.py",  # bech32 is defined lowercase
        "chains/tron.py",
        "chains/sui.py",
        "attribution/ens.py",  # Ethereum-only by definition
        "attribution/sources/ofac.py",  # guarded by _key
        "attribution/sources/etherscan_dump.py",  # an Etherscan export is EVM
        "analysis/revenue.py",  # guarded by _fold
        "providers/jsonrpc.py",
        "providers/etherscan.py",
        "providers/blockscout.py",
    }

    #: Known and wrong on non-EVM chains. This number may only go down.
    KNOWN_UNFIXED = 20

    def _offenders(self) -> list[str]:
        import re

        pattern = re.compile(
            r"address\w*\.lower\(\)|\.lower\(\) for \w*address|seed\.lower\(\)"
        )
        found = []
        for path in (self.ROOT / "src").rglob("*.py"):
            relative = str(path.relative_to(self.ROOT / "src" / "chainscope"))
            if relative in self.EVM_ONLY:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{relative}:{number}")
        return sorted(found)

    def test_no_new_site_folds_case_by_hand(self) -> None:
        offenders = self._offenders()
        assert len(offenders) <= self.KNOWN_UNFIXED, (
            f"{len(offenders)} sites lowercase an address outside the EVM-only "
            f"files, up from {self.KNOWN_UNFIXED}. Use "
            f"chainscope.chains.address_key.\n  " + "\n  ".join(offenders)
        )

    def test_the_count_is_kept_honest(self) -> None:
        """Lower the constant when sites are fixed, so the debt is visible.

        A ratchet that silently allows slack stops being a ratchet.
        """
        offenders = self._offenders()
        assert len(offenders) == self.KNOWN_UNFIXED, (
            f"{len(offenders)} sites remain but KNOWN_UNFIXED says "
            f"{self.KNOWN_UNFIXED}. Update it --- downwards."
        )

    def test_the_extending_guide_does_not_teach_it(self) -> None:
        # The guide is where a plugin author copies from, so a wrong example
        # there propagates further than a wrong line anywhere else.
        guide = (self.ROOT / "docs" / "extending.md").read_text()
        assert "address.lower()" not in guide
        assert "address_key" in guide
