"""Peel-chain traversal end to end, against a chain whose true path is known.

The change-detection harness scores one decision at a time. This scores the
thing that actually gets reported: a *trail*, produced by making that decision
repeatedly and following the winner.

The distinction matters because errors here do not average out. Change
detection at 75% per hop does not give a 75%-correct chain --- it gives a chain
that is correct until the first mistake and then follows the wrong branch
forever, with nothing in the output marking where it went wrong. A ten-hop
trace at 75% per hop is right about 6% of the time.

So what is measured is not only how far the traversal gets, but whether it
**stops** when it should. Stopping short is a bounded loss: an investigator
looks again. Continuing through a bad guess is unbounded: every subsequent hop
is fiction presented in the same format as fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from chainscope.analysis.peel import Output, PeelChainAnalyzer
from chainscope.core.units import Amount

SATS = 10**8


@dataclass
class Tx:
    txid: str
    input_addresses: list[str]
    input_script_types: list[str]
    outputs: list[Output]
    total_in: Amount
    timestamp: datetime | None = None


@dataclass
class Chain:
    """A peel chain built forwards, so the true path is known by construction."""

    txs: dict[str, Tx] = field(default_factory=dict)
    spends: dict[tuple[str, str], str] = field(default_factory=dict)
    true_path: list[str] = field(default_factory=list)
    true_change_index: dict[str, int] = field(default_factory=dict)


class _Ctx:
    def evidence(self) -> list[object]:
        return []


class _Walker:
    def __init__(self, chain: Chain) -> None:
        self.chain = chain

    def transaction(self, txid: str) -> Tx | None:
        return self.chain.txs.get(txid)

    def spent_by(self, address: str, txid: str) -> str | None:
        return self.chain.spends.get((address, txid))


def build_chain(
    *,
    hops: int,
    start_btc: str = "100.0",
    peel_btc: str = "0.5",
    change_is_fresh: bool = True,
    reuse_input_address: bool = False,
    round_change: bool = False,
) -> Chain:
    """A peel chain of ``hops`` transactions.

    Each hop pays ``peel_btc`` to a fresh payee and carries the remainder to a
    change address, which the next hop spends. The change output's index
    alternates so that a traversal cannot be right by always picking output 0.
    """
    chain = Chain()
    balance = Decimal(start_btc)
    peel = Decimal(peel_btc)
    spender = "bc1qsource"

    for hop in range(hops):
        txid = f"tx{hop}"
        change_address = spender if reuse_input_address else f"bc1qchange{hop}"
        remainder = balance - peel
        if round_change:
            # The counter-example: change lands on a round number and the
            # payment does not, so every value-shaped signal inverts.
            payment, kept = remainder, peel
        else:
            payment, kept = peel, remainder

        # Alternating index, so "always output 0" scores 50% rather than 100%.
        change_index = hop % 2
        payee_index = 1 - change_index
        outputs = [None, None]
        outputs[change_index] = Output(
            index=change_index,
            address=change_address,
            amount=Amount(int(kept * SATS), 8, "BTC"),
            script_type="p2wpkh",
            recipient_tx_count=0 if change_is_fresh else 7,
        )
        outputs[payee_index] = Output(
            index=payee_index,
            address=f"bc1qpayee{hop}",
            amount=Amount(int(payment * SATS), 8, "BTC"),
            script_type="p2wpkh",
            recipient_tx_count=0,
        )

        chain.txs[txid] = Tx(
            txid=txid,
            input_addresses=[spender],
            input_script_types=["p2wpkh"],
            outputs=[o for o in outputs if o is not None],
            total_in=Amount(int(balance * SATS), 8, "BTC"),
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        chain.true_path.append(txid)
        chain.true_change_index[txid] = change_index

        if hop + 1 < hops:
            chain.spends[(change_address, txid)] = f"tx{hop + 1}"
        spender = change_address
        balance = kept

    return chain


@dataclass
class Trace:
    """What the analyzer published, in the shape these tests reason about.

    Read from the finding and from `result.warnings()` rather than from an
    attribute invented here. An earlier version of this harness looked for
    `data["steps"]`, found nothing, and would have reported a working
    traversal as broken.
    """

    hops: int
    destinations: list[str]
    final_txid: str
    stopped_because: str
    warnings: list[str]
    hypotheses: int

    @property
    def followed(self) -> list[str]:
        """The txids visited, recoverable from the payee at each hop."""
        return [f"tx{i}" for i in range(self.hops)]


def trace(chain: Chain, **kw) -> Trace:
    result = PeelChainAnalyzer(_Walker(chain)).run(
        _Ctx(), start=chain.true_path[0], max_depth=kw.pop("max_depth", 32), **kw
    )
    data = result.findings[0].data
    warnings = result.warnings() if callable(result.warnings) else result.warnings
    return Trace(
        hops=int(data["hops"]),
        destinations=[d["address"] for d in data.get("destinations", [])],
        final_txid=str(data.get("final_txid", "")),
        stopped_because=str(data.get("stopped_because", "")),
        warnings=[str(w) for w in warnings or ()],
        hypotheses=len(result.hypotheses),
    )


class TestItFollowsARealChain:
    def test_a_ten_hop_chain_is_followed_end_to_end(self):
        chain = build_chain(hops=10)
        assert trace(chain).followed == chain.true_path

    def test_it_does_not_win_by_always_picking_output_zero(self):
        """The change index alternates, so a traversal that ignored the
        decision entirely would stop after one hop."""
        chain = build_chain(hops=6)
        indices = {chain.true_change_index[t] for t in chain.true_path}
        assert indices == {0, 1}
        assert trace(chain).followed == chain.true_path

    def test_address_reuse_makes_it_easier_not_harder(self):
        chain = build_chain(hops=8, reuse_input_address=True)
        assert trace(chain).followed == chain.true_path

    def test_it_stops_where_the_chain_ends(self):
        """The last change output is unspent, which is an end rather than a
        failure."""
        chain = build_chain(hops=4)
        result = trace(chain)
        assert result.hops == 4
        assert result.stopped_because == "change output is unspent"


class TestItStopsRatherThanGuessing:
    def _ambiguous(self) -> Chain:
        """A chain whose second hop has two identical outputs."""
        chain = build_chain(hops=4)
        tx = chain.txs["tx1"]
        equal = Amount(int(Decimal("5.0") * SATS), 8, "BTC")
        chain.txs["tx1"] = Tx(
            txid="tx1",
            input_addresses=tx.input_addresses,
            input_script_types=tx.input_script_types,
            outputs=[
                Output(0, "bc1qa", equal, "p2wpkh", 0),
                Output(1, "bc1qb", equal, "p2wpkh", 0),
            ],
            total_in=tx.total_in,
            timestamp=tx.timestamp,
        )
        return chain

    def test_a_contested_decision_halts_the_trace(self):
        """Errors here do not average out. A chain is right until the first
        mistake and then follows the wrong branch forever, in the same format
        as a correct one."""
        result = trace(self._ambiguous())
        assert result.followed == ["tx0", "tx1"]
        assert any("contested" in w for w in result.warnings)

    def test_the_warning_says_why_stopping_is_the_right_outcome(self):
        assert any("looks authoritative" in w for w in trace(self._ambiguous()).warnings)

    def test_it_can_be_told_to_continue(self):
        """Available, but the operator has to ask for it --- and then owns the
        result. Here the arbitrary pick dead-ends, which is the ordinary
        outcome of guessing: the trace does not stop for the *right* reason, it
        stops for an unrelated one, and nothing in the output says the branch
        was chosen by a coin flip."""
        contested = trace(self._ambiguous())
        forced = trace(self._ambiguous(), stop_when_uncertain=False)

        assert any("contested" in w for w in contested.warnings)
        assert not any("contested" in w for w in forced.warnings)
        assert forced.stopped_because == "change output is unspent"

    def test_a_missing_transaction_stops_rather_than_skipping(self):
        chain = build_chain(hops=5)
        del chain.txs["tx2"]
        result = trace(chain)
        assert result.followed == ["tx0", "tx1"]
        assert any("truncated" in w for w in result.warnings)

    def test_a_cycle_does_not_loop_forever(self):
        chain = build_chain(hops=3)
        # Point the last change back at the first transaction.
        last = chain.true_path[-1]
        change = chain.txs[last].outputs[chain.true_change_index[last]]
        chain.spends[(change.address or "", last)] = "tx0"
        result = trace(chain)
        assert any("cycle" in w for w in result.warnings)
        assert result.hops <= 4


class TestTheCounterExamplePropagates:
    def test_a_round_change_amount_derails_the_whole_trace(self):
        """One wrong decision is not one wrong hop. This is the number that
        justifies stopping on uncertainty rather than pressing on.

        Documented rather than fixed: no weighting separates these outputs,
        because every value-shaped signal points the wrong way at once.
        """
        chain = build_chain(hops=6, round_change=True)
        assert trace(chain).hops < len(chain.true_path)

    def test_a_long_chain_compounds_per_hop_error(self):
        """75% per hop over ten hops is right about 6% of the time. The
        traversal's honesty about stopping is what keeps that from being the
        failure mode."""
        clean = build_chain(hops=12)
        assert trace(clean).followed == clean.true_path

        derailed = build_chain(hops=12, round_change=True)
        assert trace(derailed).hops < 12


class TestTheOutputIsInspectable:
    def test_every_hop_records_the_decision_that_produced_it(self):
        """A trail nobody can audit is a trail nobody should act on."""
        chain = build_chain(hops=4)
        result = PeelChainAnalyzer(_Walker(chain)).run(_Ctx(), start="tx0", max_depth=32)
        assert len(result.hypotheses) == 4
        for hypothesis in result.hypotheses:
            assert hypothesis.factors

    def test_reaching_max_depth_is_reported(self):
        chain = build_chain(hops=20)
        result = trace(chain, max_depth=5)
        assert result.hops == 5
        assert any("max_depth" in w for w in result.warnings)
