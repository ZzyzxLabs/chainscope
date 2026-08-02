"""Co-spend clustering.

The critical test is that a CoinJoin does not merge strangers. Because the
heuristic is transitive, one bad merge propagates through the whole cluster —
so this is not a precision nicety, it is the difference between a wallet and a
fiction.
"""

from dataclasses import dataclass, field

import pytest

from chainscope.analysis.base import Context
from chainscope.analysis.cluster import (
    CoSpendClusterAnalyzer,
    looks_like_coinjoin,
)
from chainscope.core.chainid import BITCOIN
from chainscope.core.result import Severity
from chainscope.providers.router import Router


@dataclass
class FakeSpend:
    txid: str
    input_addresses: list
    output_values: list = field(default_factory=lambda: [50_000_000, 49_000_000])


class FakeWalker:
    def __init__(self, spends: dict, fail_on: set | None = None):
        self.spends = spends
        self.fail_on = fail_on or set()

    def spending_transactions(self, address):
        if address in self.fail_on:
            raise RuntimeError("upstream error")
        return self.spends.get(address, [])


def ctx():
    return Context(chain=BITCOIN, router=Router())


class TestCoinJoinDetection:
    def test_equal_outputs_at_scale_are_a_coinjoin(self):
        assert looks_like_coinjoin(8, [10**7] * 8)

    def test_ordinary_two_output_spend_is_not(self):
        assert not looks_like_coinjoin(1, [200_000, 800_000])

    def test_a_few_equal_outputs_are_not_enough(self):
        # Batch payouts legitimately repeat amounts; three is common.
        assert not looks_like_coinjoin(2, [10**7, 10**7, 10**7, 555])

    def test_many_inputs_with_varied_outputs_is_consolidation_not_coinjoin(self):
        assert not looks_like_coinjoin(40, [123, 456, 789, 1011])

    def test_threshold_is_configurable(self):
        assert looks_like_coinjoin(3, [10**7] * 3, threshold=3)


class TestExpansion:
    def test_co_inputs_join_the_cluster(self):
        walker = FakeWalker({"A": [FakeSpend("t1", ["A", "B", "C"])]})
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert set(f.data["addresses"]) == {"A", "B", "C"}

    def test_expansion_is_transitive(self):
        walker = FakeWalker(
            {
                "A": [FakeSpend("t1", ["A", "B"])],
                "B": [FakeSpend("t2", ["B", "C"])],
                "C": [FakeSpend("t3", ["C", "D"])],
            }
        )
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert set(f.data["addresses"]) == {"A", "B", "C", "D"}

    def test_receiving_funds_does_not_imply_control(self):
        """The heuristic needs the address to be an *input*. Being paid says
        nothing about who controls the payer."""
        walker = FakeWalker({"A": [FakeSpend("t1", ["X", "Y"])]})
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert set(f.data["addresses"]) == {"A"}

    def test_isolated_address_clusters_alone(self):
        res = CoSpendClusterAnalyzer(FakeWalker({})).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert f.data["size"] == 1
        assert f.severity is Severity.INFO


class TestCoinJoinHandling:
    def _mixed(self):
        return FakeWalker(
            {
                "A": [
                    FakeSpend("clean", ["A", "B"]),
                    FakeSpend("cj", ["A", "S1", "S2", "S3", "S4", "S5"], [10**7] * 6),
                ]
            }
        )

    def test_coinjoin_does_not_merge_strangers(self):
        res = CoSpendClusterAnalyzer(self._mixed()).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert set(f.data["addresses"]) == {"A", "B"}
        assert "S1" not in f.data["addresses"]

    def test_skipping_is_disclosed(self):
        res = CoSpendClusterAnalyzer(self._mixed()).run(ctx(), address="A")
        assert any("suspected CoinJoin" in w for w in res.warnings)
        assert any("propagates transitively" in w for w in res.warnings)

    def test_skipped_txids_are_listed(self):
        res = CoSpendClusterAnalyzer(self._mixed()).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert f.data["skipped_coinjoins"] == ["cj"]

    def test_opting_out_merges_them_as_the_user_asked(self):
        res = CoSpendClusterAnalyzer(self._mixed()).run(
            ctx(), address="A", skip_coinjoins=False
        )
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert "S1" in f.data["addresses"]


class TestLimits:
    def test_address_limit_marks_the_result_as_a_lower_bound(self):
        walker = FakeWalker(
            {f"A{i}": [FakeSpend(f"t{i}", [f"A{i}", f"A{i + 1}"])] for i in range(50)}
        )
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A0", max_addresses=5)
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert f.data["truncated"]
        assert any("lower bound" in w for w in res.warnings)

    def test_transaction_limit_is_reported(self):
        walker = FakeWalker({"A": [FakeSpend(f"t{i}", ["A", f"B{i}"]) for i in range(100)]})
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A", max_transactions=10)
        assert any("limits" in w for w in res.warnings)

    def test_a_failing_lookup_does_not_end_the_walk(self):
        walker = FakeWalker(
            {"A": [FakeSpend("t1", ["A", "B", "C"])]},
            fail_on={"B"},
        )
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert "C" in f.data["addresses"]
        assert any("could not enumerate B" in w for w in res.warnings)


class TestInterpretation:
    def test_detail_refuses_to_claim_identity(self):
        """A cluster shows shared control. It does not say by whom."""
        walker = FakeWalker({"A": [FakeSpend("t1", ["A", "B"])]})
        res = CoSpendClusterAnalyzer(walker).run(ctx(), address="A")
        (f,) = [f for f in res.findings if "cluster of" in f.title]
        assert "does not identify the controller" in f.detail

    def test_contract(self):
        with pytest.raises(ValueError, match="needs an `address`"):
            CoSpendClusterAnalyzer(FakeWalker({})).run(ctx())
        with pytest.raises(ValueError, match="no walker configured"):
            CoSpendClusterAnalyzer().run(ctx(), address="A")
