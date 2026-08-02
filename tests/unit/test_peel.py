"""Peel chains and change detection.

Getting change wrong once sends the trail after a payment while the funds walk
away — and the output looks identical either way. So the tests that matter are
the ones checking that ambiguity stops the walk instead of being papered over.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chainscope.analysis.base import Context
from chainscope.analysis.peel import (
    Output,
    PeelChainAnalyzer,
    detect_change,
)
from chainscope.core.chainid import BITCOIN
from chainscope.core.result import Severity
from chainscope.core.units import Amount
from chainscope.providers.router import Router

MINE = "bc1qmine"
PAYEE = "bc1qpayee"


def out(index, btc, address=None, script="p2wpkh", recip_txs=-1):
    return Output(
        index=index,
        address=address,
        amount=Amount.parse(btc, 8, "BTC"),
        script_type=script,
        recipient_tx_count=recip_txs,
    )


@dataclass
class FakeTx:
    outputs: list
    input_addresses: list = field(default_factory=lambda: [MINE])
    input_script_types: list = field(default_factory=lambda: ["p2wpkh"])
    total_in: Amount = field(default_factory=lambda: Amount.parse("10", 8, "BTC"))
    timestamp: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )


class FakeWalker:
    def __init__(self, chain: dict, spends: dict | None = None):
        self.chain = chain
        self.spends = spends or {}

    def transaction(self, txid):
        return self.chain.get(txid)

    def spent_by(self, address, after_txid):
        return self.spends.get((address, after_txid))


def ctx():
    return Context(chain=BITCOIN, router=Router())


class TestChangeDetection:
    def test_address_reuse_is_near_conclusive(self):
        d = detect_change(
            [out(0, "2", PAYEE, recip_txs=0), out(1, "8.33", MINE)],
            {MINE},
            {"p2wpkh"},
        )
        assert d.index == 1
        assert d.confident
        names = {f.name for f in d.hypothesis.factors if f.contribution > 0}
        assert "pays_back_into_input_set" in names

    def test_round_number_marks_the_payment(self):
        """Humans pay round amounts; wallets do not choose their change."""
        d = detect_change(
            [out(0, "2", PAYEE), out(1, "8.33478337", "bc1qchange")],
            set(),
            {"p2wpkh"},
        )
        assert d.index == 1

    def test_fresh_recipient_is_treated_as_the_payee(self):
        d = detect_change(
            [
                out(0, "1.234", "bc1qnew", recip_txs=0),
                out(1, "1.235", "bc1qold", recip_txs=400),
            ],
            set(),
            {"p2wpkh"},
        )
        assert d.index == 1

    def test_script_type_mismatch_points_outward(self):
        d = detect_change(
            [
                out(0, "1.111", PAYEE, script="p2pkh"),
                out(1, "1.112", "bc1qchange", script="p2wpkh"),
            ],
            set(),
            {"p2wpkh"},
        )
        assert d.index == 1

    def test_single_output_needs_no_decision(self):
        d = detect_change([out(0, "5", MINE)], {MINE}, {"p2wpkh"})
        assert d.index == 0 and d.confident

    def test_no_outputs(self):
        d = detect_change([], set(), set())
        assert d.index is None and not d.confident

    def test_indistinguishable_outputs_are_contested(self):
        """Two identical-looking outputs must not be resolved by tiebreak luck."""
        d = detect_change(
            [out(0, "5", "bc1qa"), out(1, "5", "bc1qb")],
            set(),
            {"p2wpkh"},
        )
        assert d.hypothesis.is_contested
        assert not d.confident

    def test_alternatives_are_recorded(self):
        d = detect_change(
            [out(0, "2", PAYEE, recip_txs=0), out(1, "8.33", MINE)],
            {MINE},
            {"p2wpkh"},
        )
        assert d.hypothesis.alternatives


class TestWalk:
    def _chain(self, hops: int):
        """A textbook peel: 2 BTC shed per hop, change carries on."""
        chain, spends = {}, {}
        remaining = Decimal("100")
        for i in range(hops):
            txid = f"tx{i}"
            change_addr = f"bc1qchange{i}"
            remaining -= 2
            chain[txid] = FakeTx(
                outputs=[
                    out(0, "2", f"bc1qpayee{i}", recip_txs=0),
                    out(1, str(remaining), change_addr),
                ],
                input_addresses=[f"bc1qchange{i - 1}" if i else MINE],
                total_in=Amount.parse(str(remaining + 2), 8, "BTC"),
            )
            if i + 1 < hops:
                spends[(change_addr, txid)] = f"tx{i + 1}"
        return FakeWalker(chain, spends)

    def test_follows_the_chain(self):
        res = PeelChainAnalyzer(self._chain(5)).run(ctx(), start="tx0", max_depth=10)
        (f,) = [f for f in res.findings if "peel chain" in f.title]
        assert f.data["hops"] == 5
        assert f.data["peeled_raw"] == 5 * 2 * 10**8
        assert len(f.data["destinations"]) == 5

    def test_stops_when_change_is_unspent(self):
        res = PeelChainAnalyzer(self._chain(3)).run(ctx(), start="tx0", max_depth=10)
        (f,) = [f for f in res.findings if "peel chain" in f.title]
        assert f.data["stopped_because"] == "change output is unspent"

    def test_max_depth_is_reported(self):
        """Silent truncation would make a partial trail look complete."""
        res = PeelChainAnalyzer(self._chain(10)).run(ctx(), start="tx0", max_depth=3)
        assert any("max_depth=3" in w for w in res.warnings)

    def test_cycle_is_detected(self):
        walker = FakeWalker(
            {"a": FakeTx(outputs=[out(0, "1", PAYEE), out(1, "9", "bc1qloop")])},
            {("bc1qloop", "a"): "a"},
        )
        res = PeelChainAnalyzer(walker).run(ctx(), start="a", max_depth=10)
        assert any("cycle detected" in w for w in res.warnings)

    def test_missing_transaction_is_reported(self):
        res = PeelChainAnalyzer(FakeWalker({})).run(ctx(), start="ghost")
        assert any("could not retrieve" in w for w in res.warnings)

    def test_min_peel_filters_dust(self):
        walker = FakeWalker(
            {
                "a": FakeTx(
                    outputs=[
                        out(0, "0.00001", "bc1qdust", recip_txs=0),
                        out(1, "2", PAYEE, recip_txs=0),
                        out(2, "7.9", MINE),
                    ]
                )
            }
        )
        res = PeelChainAnalyzer(walker).run(ctx(), start="a", min_peel="0.001")
        (f,) = [f for f in res.findings if "peel chain" in f.title]
        assert all(d["address"] != "bc1qdust" for d in f.data["destinations"])


class TestAmbiguityHandling:
    def _ambiguous(self):
        return FakeWalker({"a": FakeTx(outputs=[out(0, "5", "bc1qa"), out(1, "5", "bc1qb")])})

    def test_walk_stops_at_an_ambiguous_hop(self):
        res = PeelChainAnalyzer(self._ambiguous()).run(ctx(), start="a")
        assert any("contested" in w for w in res.warnings)
        assert any("looks authoritative and is wrong" in w for w in res.warnings)

    def test_ambiguity_is_raised_as_an_important_finding(self):
        res = PeelChainAnalyzer(self._ambiguous()).run(ctx(), start="a")
        amb = [f for f in res.findings if "ambiguous change" in f.title]
        assert amb and amb[0].severity is Severity.IMPORTANT
        assert "coin flip" in amb[0].detail

    def test_continuing_past_ambiguity_is_opt_in(self):
        res = PeelChainAnalyzer(self._ambiguous()).run(
            ctx(), start="a", stop_when_uncertain=False
        )
        assert not any("stopped at depth" in w for w in res.warnings)


class TestContract:
    def test_start_is_required(self):
        with pytest.raises(ValueError, match="needs a `start` txid"):
            PeelChainAnalyzer(FakeWalker({})).run(ctx())

    def test_walker_is_required(self):
        with pytest.raises(ValueError, match="no walker configured"):
            PeelChainAnalyzer().run(ctx(), start="a")

    def test_params_capture_the_run(self):
        res = PeelChainAnalyzer(FakeWalker({})).run(ctx(), start="a", max_depth=4)
        assert res.params["start"] == "a"
        assert res.params["max_depth"] == 4
