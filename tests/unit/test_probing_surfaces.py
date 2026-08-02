"""Probing reachable from where people actually work.

The technique was implemented, measured, and invisible: usable from Python and
absent from both the CLI and the agent. That is the safe direction to fail in
and still a failure --- this session already named it once, in the analyzer
entry points that pointed at bare functions.

So these tests are about reach, not about the algorithm. The algorithm is
scored in tests/validation/test_probing_detection_accuracy.py.
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
OPERATOR = "0x" + "1" * 40
DEPOSIT = "0x" + "2" * 40
TRADEOGRE = (5, 10, 20, 30, 50, 75, 100, 125, 150, 175)


@pytest.fixture
def store_path(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, OPERATOR, OPERATOR),
                recipient=Address(ETHEREUM, DEPOSIT, DEPOSIT),
                amount=Amount(n * ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=1000 + i,
                index=0,
            )
            for i, n in enumerate(TRADEOGRE)
        ],
        source="t",
    )
    store.close()
    return tmp_path / "s.db"


def call(path, name, args):
    server = build_server(ServerConfig(store=path))
    out = asyncio.run(server.call_tool(name, args))
    text = out.content[0].text if hasattr(out, "content") else str(out)
    return json.loads(text)


class TestTheCliSeesIt:
    def test_it_is_a_registered_analyzer(self):
        from chainscope.cli.commands.analyze import available

        assert "probing" in available()

    def test_it_satisfies_the_entry_point_contract(self):
        """The same check that caught two bare functions earlier."""
        from chainscope.analysis.base import Analyzer
        from chainscope.cli.commands.analyze import available, rejected

        assert issubclass(available()["probing"], Analyzer)
        assert "probing" not in rejected()

    def test_it_refuses_without_an_address_rather_than_returning_nothing(self):
        from chainscope.analysis.probing import ProbingAnalyzer

        with pytest.raises(ValueError, match="address"):
            ProbingAnalyzer().run(None, address="")


class TestTheAgentSeesIt:
    def test_the_tool_is_registered(self, store_path):
        server = build_server(ServerConfig(store=store_path))
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert "find_probes" in names

    def test_it_finds_the_recorded_sequence(self, store_path):
        result = call(store_path, "find_probes", {"address": OPERATOR})
        assert result["transfers_examined"] == 10
        probe = result["probes"][0]
        assert probe["kind"] == "escalation"
        assert probe["steps"] == 10
        assert probe["growth"] == pytest.approx(35.0)

    def test_an_empty_result_says_it_is_not_evidence_of_absence(self, store_path):
        """The distinction an agent will otherwise flatten: "we looked and found
        nothing" reads as "there is nothing" unless the reply says otherwise."""
        result = call(store_path, "find_probes", {"address": "0x" + "9" * 40})
        assert result["probes"] == []
        assert "not evidence of absence" in result["note"]

    def test_a_clipped_window_is_reported(self, store_path):
        """A probe is a sequence, so a truncated read shortens runs and can drop
        a real one below the floor with nothing looking wrong."""
        result = call(store_path, "find_probes", {"address": OPERATOR, "limit": 5})
        assert "truncated" in result

    def test_a_missing_address_is_refused(self, store_path):
        with pytest.raises(Exception, match="address"):
            call(store_path, "find_probes", {"address": "  "})

    def test_it_reads_only_the_store(self, store_path):
        """No network: the suite blocks sockets, so this passing at all is the
        assertion."""
        assert call(store_path, "find_probes", {"address": OPERATOR})["probes"]
