"""Walking the store into a graph.

Three bugs here were visible in the rendered picture rather than in any log,
which is the worst place for them: a diagram is read as a conclusion.
"""

import pytest

from chainscope.cli.commands.graph import _chain, _walk
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

A = "0x" + "a" * 40
B = "0x" + "b" * 40
TEN_ETH = 10 * 10**18


def transfer(sender, recipient, raw, *, block, symbol="ETH", decimals=18, asset=None):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.TOKEN if asset else TransferKind.NATIVE,
        block=block,
        index=0,
        asset=Address(ETHEREUM, asset, asset) if asset else None,
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.db")
    s.put_transfers(
        [
            transfer(A, B, TEN_ETH, block=100),
            transfer(A, B, TEN_ETH, block=101),
            transfer(A, B, 5_000_000, block=102, symbol="USDC", decimals=6, asset="0xusdc"),
        ],
        source="t",
    )
    yield s
    s.close()


class TestDoubleCounting:
    def test_walking_both_ways_does_not_double_a_transfer(self, store):
        """A transfer is reachable from the sender's outbound edges and again
        from the recipient's inbound ones, and add_edge folds the second
        sighting into the first."""
        one = _walk(store, A, ETHEREUM, depth=2, max_nodes=50, per_node=10, direction="out")
        both = _walk(store, A, ETHEREUM, depth=2, max_nodes=50, per_node=10, direction="both")
        assert one.totals_by_asset() == both.totals_by_asset()

    def test_counts_are_not_doubled_either(self, store):
        both = _walk(store, A, ETHEREUM, depth=2, max_nodes=50, per_node=10, direction="both")
        eth = next(e for e in both.edges.values() if e.symbol == "ETH")
        assert eth.transfer_count == 2


class TestAmountsCanBeRendered:
    def test_a_native_edge_names_its_asset(self, store):
        """A blank symbol leaves a renderer nothing to print, and the fallback
        it would invent -- the contract address -- is not a name."""
        graph = _walk(store, A, ETHEREUM, depth=1, max_nodes=50, per_node=10, direction="out")
        eth = next(e for e in graph.edges.values() if e.total_raw == TEN_ETH * 2)
        assert eth.symbol == "ETH"
        assert eth.decimals == 18

    def test_a_token_edge_carries_its_own_decimals(self, store):
        """USDC rendered at eighteen is a trillion times too small."""
        graph = _walk(store, A, ETHEREUM, depth=1, max_nodes=50, per_node=10, direction="out")
        usdc = next(e for e in graph.edges.values() if e.symbol == "USDC")
        assert usdc.decimals == 6


class TestAttributionsAreChainScoped:
    def _labelled(self, store, chain):
        store.put_attributions(
            [
                Attribution(
                    label="PancakeSwap",
                    category=Category.DEX,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="bscscan",
                    address=B,
                    chain=chain,
                )
            ]
        )
        return _walk(store, A, ETHEREUM, depth=1, max_nodes=50, per_node=10, direction="out")

    def test_another_chains_label_does_not_leak(self, store):
        """An address string is not unique across chains: the same twenty bytes
        exist on Ethereum and BSC, and a BSC claim says nothing about the
        Ethereum address sharing its hex."""
        graph = self._labelled(store, BSC)
        assert graph.nodes[f"{ETHEREUM}:{B}"].label == ""

    def test_this_chains_label_is_used(self, store):
        graph = self._labelled(store, ETHEREUM)
        assert graph.nodes[f"{ETHEREUM}:{B}"].label == "PancakeSwap"

    def test_a_chain_agnostic_claim_applies_everywhere(self, store):
        """Sanctions lists are published against the address, not the chain."""
        graph = self._labelled(store, None)
        assert graph.nodes[f"{ETHEREUM}:{B}"].label == "PancakeSwap"


class TestChainParsing:
    @pytest.mark.parametrize("raw", ["1", "56", "eip155:1", "sui:mainnet"])
    def test_valid_forms(self, raw):
        assert _chain(raw) is not None

    @pytest.mark.parametrize("bad", ["bsc", "ethereum", "oops", ""])
    def test_a_malformed_chain_is_refused(self, bad):
        """Resolving to "unspecified" would be reinterpreted downstream as
        Ethereum, and the caller would get a confident answer about a chain
        they did not ask about."""
        with pytest.raises(ValueError, match="not a chain id"):
            _chain(bad)

    def test_the_message_shows_both_accepted_forms(self):
        with pytest.raises(ValueError) as exc:
            _chain("bsc")
        assert "eip155:1" in str(exc.value)
