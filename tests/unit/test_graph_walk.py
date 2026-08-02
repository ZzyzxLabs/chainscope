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


C = "0x" + "c" * 40
D = "0x" + "d" * 40


@pytest.fixture
def chain_store(tmp_path):
    """A → B → C → D, so a depth-limited walk has somewhere left to go."""
    s = SqliteStore(tmp_path / "chain.db")
    s.put_transfers(
        [
            transfer(A, B, TEN_ETH, block=100),
            transfer(B, C, TEN_ETH, block=101),
            transfer(C, D, TEN_ETH, block=102),
        ],
        source="t",
    )
    yield s
    s.close()


class TestTheFrontierIsHonest:
    """``expanded`` claims this address's counterparties were fetched. The last
    ring of a depth-limited walk was marked expanded before anything read its
    edges, so an address with five hundred onward transfers rendered as a leaf
    --- indistinguishable from one that genuinely had nowhere to go. That is the
    visual form of the failure this project exists to prevent."""

    def test_the_last_ring_is_frontier_not_leaf(self, chain_store):
        graph = _walk(
            chain_store, A, ETHEREUM, depth=1, max_nodes=50, per_node=10, direction="out"
        )
        b = graph.nodes[f"{ETHEREUM}:{B.lower()}"]
        assert b.is_frontier, "B's edges were never read; it must not claim to be expanded"

    def test_an_address_whose_edges_were_read_is_expanded(self, chain_store):
        graph = _walk(
            chain_store, A, ETHEREUM, depth=2, max_nodes=50, per_node=10, direction="out"
        )
        assert not graph.nodes[f"{ETHEREUM}:{B.lower()}"].is_frontier
        # C is now the last ring: reached, but nobody looked past it.
        assert graph.nodes[f"{ETHEREUM}:{C.lower()}"].is_frontier

    def test_a_genuine_dead_end_is_not_a_frontier(self, chain_store):
        """The distinction only means something if the other side holds: D has
        no outbound edges, and a walk deep enough to look must say so."""
        graph = _walk(
            chain_store, A, ETHEREUM, depth=5, max_nodes=50, per_node=10, direction="out"
        )
        assert not graph.nodes[f"{ETHEREUM}:{D.lower()}"].is_frontier

    def test_the_graph_reports_having_stopped_early(self, chain_store):
        graph = _walk(
            chain_store, A, ETHEREUM, depth=1, max_nodes=50, per_node=10, direction="out"
        )
        assert graph.frontier(), "a depth-limited walk with somewhere to go has a frontier"


def token(sender, recipient, raw, *, block, symbol, decimals, asset):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.TOKEN,
        block=block,
        index=0,
        asset=Address(ETHEREUM, asset, asset),
    )


class TestPerNodeTruncationIsRecorded:
    """`per_node` silently dropped the counterparties beyond the cap. Five of
    twenty rendered as the whole picture --- and this module's docstring names
    that as the failure it exists to prevent."""

    @pytest.fixture
    def wide(self, tmp_path):
        s = SqliteStore(tmp_path / "wide.db")
        s.put_transfers(
            [transfer(A, f"0x{i:040x}", TEN_ETH, block=i) for i in range(1, 21)],
            source="t",
        )
        yield s
        s.close()

    def test_exceeding_the_cap_sets_truncated(self, wide):
        graph = _walk(wide, A, ETHEREUM, depth=1, max_nodes=100, per_node=5, direction="out")
        assert graph.truncated

    def test_the_summary_carries_it(self, wide):
        graph = _walk(wide, A, ETHEREUM, depth=1, max_nodes=100, per_node=5, direction="out")
        assert graph.summary()["truncated"]

    def test_staying_under_the_cap_does_not(self, wide):
        """The flag has to mean something, so it must not always be set."""
        graph = _walk(wide, A, ETHEREUM, depth=1, max_nodes=100, per_node=50, direction="out")
        assert not graph.truncated

    def test_exactly_at_the_cap_is_not_truncated(self, wide):
        graph = _walk(wide, A, ETHEREUM, depth=1, max_nodes=100, per_node=20, direction="out")
        assert not graph.truncated


class TestRankingDoesNotMixUnits:
    """Raw amounts compare only within one asset. Ranking across them let
    0.001 of an 18-decimal token outrank 5,000 USDC and consume the budget ---
    and minting a worthless token with a huge supply is the deliberate version
    of that."""

    @pytest.fixture
    def mixed(self, tmp_path):
        s = SqliteStore(tmp_path / "mixed.db")
        rows = [
            token(A, f"0x{i:040x}", 10**21, block=i, symbol="SHIB", decimals=18, asset="0xshib")
            for i in range(1, 11)
        ]
        rows += [
            token(
                A,
                f"0x{i:040x}",
                5000 * 10**6,
                block=i,
                symbol="USDC",
                decimals=6,
                asset="0xusdc",
            )
            for i in range(11, 16)
        ]
        s.put_transfers(rows, source="t")
        yield s
        s.close()

    def test_the_stablecoin_is_not_crowded_out(self, mixed):
        graph = _walk(mixed, A, ETHEREUM, depth=1, max_nodes=100, per_node=4, direction="out")
        assert {e.symbol for e in graph.edges.values()} == {"SHIB", "USDC"}

    def test_the_cap_is_still_respected(self, mixed):
        graph = _walk(mixed, A, ETHEREUM, depth=1, max_nodes=100, per_node=4, direction="out")
        assert len(graph.edges) == 4

    def test_and_it_says_it_truncated(self, mixed):
        graph = _walk(mixed, A, ETHEREUM, depth=1, max_nodes=100, per_node=4, direction="out")
        assert graph.truncated
