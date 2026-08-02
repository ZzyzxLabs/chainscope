"""Taint reachable from the CLI and the agent.

The module is scored in tests/validation/test_taint_dilution.py. This is about
reach, and about the two distinctions that have to survive the trip to a
caller: FIFO's dependence on arrival order, and the difference between holding
tainted value and having passed it along.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from chainscope.agent.server import ServerConfig, build_server
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

ETH = 10**18
THIEF = "0x" + "1" * 40
RELAY = "0x" + "2" * 40
FINAL = "0x" + "3" * 40


@pytest.fixture
def store_path(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, s, s),
                recipient=Address(ETHEREUM, r, r),
                amount=Amount(10 * ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=100 + i,
                index=0,
            )
            for i, (s, r) in enumerate([(THIEF, RELAY), (RELAY, FINAL)])
        ],
        source="t",
    )
    store.close()
    return tmp_path / "s.db"


def call(path, name, args):
    server = build_server(ServerConfig(store=path))
    out = asyncio.run(server.call_tool(name, args))
    return json.loads(out.content[0].text if hasattr(out, "content") else str(out))


class TestTheCliSeesIt:
    def test_it_is_a_registered_analyzer(self):
        from chainscope.cli.commands.analyze import available, rejected

        assert "taint" in available()
        assert "taint" not in rejected()

    def test_it_refuses_without_a_source(self):
        from chainscope.analysis.taint import TaintAnalyzer

        with pytest.raises(ValueError, match="source"):
            TaintAnalyzer().run(None, source="")

    def test_an_unknown_policy_is_refused_by_name(self):
        from chainscope.analysis.taint import TaintAnalyzer

        with pytest.raises(ValueError, match="fifo"):
            TaintAnalyzer().run(None, source="0xa", policy="nonsense")


class TestTheAgentSeesIt:
    def test_the_tool_is_registered(self, store_path):
        server = build_server(ServerConfig(store=store_path))
        assert "trace_stolen_funds" in {t.name for t in asyncio.run(server.list_tools())}

    def test_it_follows_the_value_to_the_end(self, store_path):
        out = call(store_path, "trace_stolen_funds", {"source": THIEF, "amount": str(10 * ETH)})
        assert out["addresses"][FINAL] == str(10 * ETH)

    def test_a_relay_is_reported_separately_from_a_holder(self, store_path):
        """The distinction an agent will otherwise flatten. Reporting a relay as
        a holder is how a payment processor gets described as a launderer."""
        out = call(store_path, "trace_stolen_funds", {"source": THIEF, "amount": str(10 * ETH)})
        assert RELAY not in out["addresses"]
        assert RELAY in out["passed_through_but_holds_none"]

    def test_amounts_come_back_as_strings(self, store_path):
        """10 ETH is 1e19, past 2^53."""
        out = call(store_path, "trace_stolen_funds", {"source": THIEF, "amount": str(10 * ETH)})
        assert isinstance(out["total_tainted"], str)

    def test_a_clipped_window_warns_about_order_not_just_reach(self, store_path):
        """FIFO depends on arrival order, so truncation changes which funds
        paid for what --- a different failure from missing a hop."""
        out = call(
            store_path,
            "trace_stolen_funds",
            {"source": THIEF, "amount": str(10 * ETH), "limit": 1},
        )
        assert "arrival order" in out["truncated"]

    def test_a_non_fifo_policy_carries_its_own_warning(self, store_path):
        out = call(store_path, "trace_stolen_funds", {"source": THIEF, "policy": "poison"})
        assert "invents value never stolen" in out["policy_warning"]

    def test_fifo_carries_no_such_warning(self, store_path):
        out = call(store_path, "trace_stolen_funds", {"source": THIEF})
        assert "policy_warning" not in out

    def test_an_unknown_policy_is_refused(self, store_path):
        with pytest.raises(Exception, match="fifo"):
            call(store_path, "trace_stolen_funds", {"source": THIEF, "policy": "vibes"})

    def test_a_missing_source_is_refused(self, store_path):
        with pytest.raises(Exception, match="source"):
            call(store_path, "trace_stolen_funds", {"source": "  "})
