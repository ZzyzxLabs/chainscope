"""The MCP agent surface.

What is worth testing here is not that the tools return data --- it is that they
return it in a shape an agent cannot casually overstate. An agent is a confident
narrator, and a forensics tool whose output paraphrases into certainty is worse
than no tool.

So: provenance travels with every claim, absence is described rather than
implied, truncation is stated, amounts survive JSON intact, and a label written
by a model is permanently marked as such.
"""

import asyncio
import importlib.util
import json

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None, reason="needs chainscope[agent]"
)

A = "0x28c6c06298d514db089934071355e5743bf21d60"
B = "0xa160cdab225685da1d56aa342ad8841c3b53f291"
UNKNOWN = "0x" + "9" * 40

TEN_ETH = 10 * 10**18


def _raises(server, name, args) -> str:
    """Invoke a tool expected to fail, returning the message.

    The SDK surfaces a refusal as a protocol-level error rather than a normal
    result, which is the right shape: an error that arrives as data is an error
    an agent can read as a finding.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        asyncio.run(server.call_tool(name, args))
    return str(exc.value)


def _call(server, name, args):
    """Invoke a tool and return the decoded payload.

    The SDK wraps results in content blocks; tests care about the object, and
    unwrapping here keeps that detail out of every assertion.
    """
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        result = result[-1]
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if text:
            return json.loads(text)
    return content


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "store.db"
    store = SqliteStore(path)
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, A, A),
                recipient=Address(ETHEREUM, B, B),
                amount=Amount(TEN_ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=18_000_000 + i,
                index=i,
            )
            for i in range(5)
        ],
        source="test",
    )
    store.put_attributions(
        [
            Attribution(
                label="Binance 14",
                category=Category.CEX,
                confidence=Confidence.MEDIUM,
                method=Method.LABEL,
                source="explorer nametag dump",
                address=A,
                chain=ETHEREUM,
            )
        ]
    )
    store.close()
    return path


@pytest.fixture
def server(store_path):
    from chainscope.agent.server import ServerConfig, build_server

    return build_server(ServerConfig(store=store_path, writable=False))


@pytest.fixture
def writable_server(store_path):
    from chainscope.agent.server import ServerConfig, build_server

    return build_server(ServerConfig(store=store_path, writable=True, agent_name="test-agent"))


class TestSurface:
    def test_the_read_tools_are_exposed(self, server):
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert {"resolve_address", "flows", "search_transfers", "sql", "store_stats"} <= names

    def test_writing_is_off_by_default(self, server):
        """An agent that can label without being asked to is a liability."""
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert "label_address" not in names

    def test_writing_appears_only_when_enabled(self, writable_server):
        names = {t.name for t in asyncio.run(writable_server.list_tools())}
        assert "label_address" in names

    def test_every_tool_describes_itself(self, server):
        """The description is the only thing steering the model's choice."""
        for tool in asyncio.run(server.list_tools()):
            assert tool.description and len(tool.description) > 40


class TestProvenanceTravelsWithClaims:
    def test_a_claim_carries_source_and_confidence(self, server):
        got = _call(server, "resolve_address", {"address": A})
        claim = got["claims"][0]
        assert claim["source"] == "explorer nametag dump"
        assert claim["confidence"] == "MEDIUM"
        assert claim["confidence_value"] == 2

    def test_absence_is_described_rather_than_implied(self, server):
        """An empty list reads as "nothing to worry about" unless it says
        otherwise."""
        got = _call(server, "resolve_address", {"address": UNKNOWN})
        assert got["claims"] == []
        assert "not that it is unlabelled anywhere" in got["note"]
        assert "benign" in got["note"]

    def test_the_server_tells_the_model_how_to_report(self, server):
        instructions = (server.instructions or "").lower()
        assert "confidence" in instructions
        assert "truncated" in instructions


class TestNumbersSurviveTheBoundary:
    def test_amounts_leave_as_strings(self, server):
        """A JSON number is an IEEE 754 double, and 10 ETH already exceeds what
        one holds exactly."""
        got = _call(server, "search_transfers", {"address": A})
        raw = got["transfers"][0]["amount"]["raw"]
        assert isinstance(raw, str)
        assert int(raw) == TEN_ETH
        assert TEN_ETH > 2**53

    def test_flow_totals_leave_as_strings(self, server):
        got = _call(server, "flows", {"address": A})
        assert isinstance(got["flows"][0]["total"], str)
        assert int(got["flows"][0]["total"]) == TEN_ETH * 5


class TestTruncationIsStated:
    def test_a_full_page_is_reported_as_truncated(self, server):
        """An agent cannot tell a short list from a cut-off one, and will
        summarise either as "I found three"."""
        got = _call(server, "search_transfers", {"address": A, "limit": 2})
        assert got["shown"] == 2
        assert got["truncated"] is True
        assert "complete set" in got["note"]

    def test_a_short_result_is_not_marked_truncated(self, server):
        got = _call(server, "search_transfers", {"address": A, "limit": 100})
        assert got["truncated"] is False
        assert got["note"] == ""

    def test_flows_report_what_was_left_out(self, server):
        got = _call(server, "flows", {"address": A, "limit": 100})
        assert got["shown"] == got["total_available"]
        assert got["truncated"] is False


class TestWriting:
    def test_an_agent_label_is_marked_as_agent_written(self, writable_server, store_path):
        """A human reviewing this store in six months has to be able to tell a
        model's suggestion from their own judgement, and there is no way to
        recover that after the fact."""
        _call(
            writable_server,
            "label_address",
            {"address": UNKNOWN, "label": "Suspected mixer", "category": "mixer"},
        )
        store = SqliteStore(store_path)
        try:
            claim = store.attributions(UNKNOWN)[0]
        finally:
            store.close()
        assert claim.source == "agent:test-agent"
        assert claim.method is Method.INFERENCE

    def test_a_low_confidence_label_still_needs_a_rationale(self, writable_server):
        """The type system's rule holds across the agent boundary too."""
        message = _raises(
            writable_server,
            "label_address",
            {"address": UNKNOWN, "label": "Maybe a mixer", "confidence": "low"},
        )
        assert "rationale" in message.lower()

    def test_a_low_confidence_label_with_a_rationale_is_accepted(self, writable_server):
        got = _call(
            writable_server,
            "label_address",
            {
                "address": UNKNOWN,
                "label": "Maybe a mixer",
                "category": "mixer",
                "confidence": "low",
                "rationale": "equal-value outputs, no change address",
            },
        )
        assert got["recorded"]["confidence"] == "LOW"

    def test_an_agent_claim_does_not_replace_an_existing_one(self, writable_server, store_path):
        _call(
            writable_server,
            "label_address",
            {"address": A, "label": "Something else", "category": "dex"},
        )
        store = SqliteStore(store_path)
        try:
            sources = {c.source for c in store.attributions(A)}
        finally:
            store.close()
        assert "explorer nametag dump" in sources
        assert "agent:test-agent" in sources


class TestSqlSurface:
    def test_a_read_query_works(self, server):
        got = _call(server, "sql", {"query": "SELECT COUNT(*) FROM transfers"})
        assert got["rows"][0][0] == 5

    def test_an_exact_sum_survives(self, server):
        got = _call(server, "sql", {"query": "SELECT SUM(amount_raw) FROM transfers"})
        assert int(got["rows"][0][0]) == TEN_ETH * 5

    def test_a_write_is_refused(self, server):
        """The SQL tool is the widest surface an agent gets; it must not become
        a filesystem primitive."""
        message = _raises(server, "sql", {"query": "DROP TABLE transfers"})
        assert "read statements" in message or "not available" in message

    def test_file_access_is_refused(self, server):
        message = _raises(server, "sql", {"query": "SELECT * FROM read_csv('/etc/passwd')"})
        assert "not available" in message or "read_csv" in message


class TestGraphExport:
    def test_the_graph_carries_attribution(self, server):
        got = _call(server, "export_graph", {"address": A, "fmt": "d3"})
        content = json.loads(got["content"])
        labels = {n["label"] for n in content["nodes"]}
        assert "Binance 14" in labels

    def test_frontier_nodes_are_marked(self, server):
        got = _call(server, "export_graph", {"address": A})
        assert got["summary"]["frontier"] >= 1

    def test_an_unknown_format_is_rejected(self, server):
        assert "format must be" in _raises(server, "export_graph", {"address": A, "fmt": "svg"})
