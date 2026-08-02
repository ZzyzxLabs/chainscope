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


class TestLimitsCannotBeAbused:
    @pytest.mark.parametrize("limit", [-1, 0])
    def test_a_nonsense_limit_does_not_silently_drop_rows(self, server, limit):
        """limit=-1 reaches rows[:-1], dropping the last result and reporting
        the rest as complete. Zero returns nothing and reads as an empty
        answer."""
        got = _call(server, "search_transfers", {"address": A, "limit": limit})
        assert got["shown"] >= 1

    def test_a_non_numeric_amount_gets_an_actionable_error(self, server):
        """These are raw integer strings; "1.5" is a decimal somebody meant in
        ETH, and letting ValueError escape says nothing about that."""
        message = _raises(server, "search_transfers", {"address": A, "min_amount": "1.5"})
        assert "smallest unit" in message

    def test_export_graph_refuses_a_bad_format_before_doing_the_work(self, server):
        assert "format must be" in _raises(server, "export_graph", {"address": A, "fmt": "svg"})

    def test_export_graph_refuses_a_bad_direction(self, server):
        assert "direction" in _raises(
            server, "export_graph", {"address": A, "direction": "sideways"}
        )

    def test_the_resolved_chain_is_reported(self, server):
        """So an agent cannot silently attribute Ethereum edges to a chain it
        asked about and did not get."""
        got = _call(server, "export_graph", {"address": A})
        assert got["chain"] == str(ETHEREUM)


class TestViewFreshness:
    def test_a_file_backed_view_is_rebuilt_when_the_store_moves_on(self, store_path, tmp_path):
        """Opening a DuckDB path creates the file, after which "does it exist"
        answers yes and the build is skipped forever --- serving last week's
        answer as though it were current."""
        import os
        import time

        from chainscope.agent.server import ServerConfig, build_server

        view = tmp_path / "view.duckdb"
        srv = build_server(ServerConfig(store=store_path, view=view))
        assert _call(srv, "sql", {"query": "SELECT COUNT(*) FROM transfers"})["rows"][0][0] == 5

        store = SqliteStore(store_path)
        try:
            store.put_transfers(
                [
                    Transfer(
                        chain=ETHEREUM,
                        tx=TxRef(ETHEREUM, "0x" + "e" * 64),
                        sender=Address(ETHEREUM, A, A),
                        recipient=Address(ETHEREUM, B, B),
                        amount=Amount(TEN_ETH, 18, "ETH"),
                        kind=TransferKind.NATIVE,
                        block=99,
                        index=99,
                    )
                ],
                source="later",
            )
        finally:
            store.close()
        # Make the store unambiguously newer than the view on filesystems whose
        # mtime granularity is coarse.
        later = time.time() + 5
        os.utime(store_path, (later, later))

        srv2 = build_server(ServerConfig(store=store_path, view=view))
        assert (
            _call(srv2, "sql", {"query": "SELECT COUNT(*) FROM transfers"})["rows"][0][0] == 6
        )


class TestMalformedChainsAreRefused:
    @pytest.mark.parametrize("bad", ["bsc", "ethereum", "eip155", "oops"])
    def test_a_typo_does_not_become_a_different_chain(self, server, bad):
        """Returning None meant "unspecified", which flows and export_graph
        then read as Ethereum and search_transfers read as "every chain". An
        agent asking about one chain would get a confident answer about
        another."""
        assert "not a chain id" in _raises(server, "flows", {"address": A, "chain": bad})

    def test_an_absent_chain_is_still_fine(self, server):
        assert _call(server, "search_transfers", {"address": A})["shown"] >= 1


class TestSqlIsBoundedAtTheEngine:
    def test_the_cap_limits_the_fetch_not_just_the_response(self, server):
        """max_rows capped the response and not the work: fetchall() ran first,
        so a query over a large store could exhaust the process before the
        caller's limit was ever applied."""
        got = _call(server, "sql", {"query": "SELECT * FROM transfers", "limit": 2})
        assert got["shown"] == 2
        assert got["truncated"] is True

    def test_a_short_result_is_not_marked_truncated(self, server):
        got = _call(server, "sql", {"query": "SELECT * FROM transfers", "limit": 100})
        assert got["truncated"] is False


@pytest.fixture
def case_path(tmp_path):
    """A case with an open question and an overdue freeze request."""
    from datetime import datetime, timedelta, timezone

    from chainscope.case.correspondence import Ledger, Request, RequestKind
    from chainscope.case.log import CaseLog, Note, NoteKind

    path = tmp_path / "case.db"
    now = datetime.now(timezone.utc)

    log = CaseLog(path)
    log.add(
        Note(
            at=now,
            analyst="alice@lab",
            identified_by="env",
            kind=NoteKind.QUESTION,
            body="who funded the gas on the first payout?",
        )
    )
    log.add(
        Note(
            at=now,
            analyst="alice@lab",
            identified_by="env",
            kind=NoteKind.OBSERVATION,
            body="eleven fresh addresses",
        )
    )
    log.close()

    ledger = Ledger(path)
    ledger.send(
        Request(
            counterparty="Binance",
            kind=RequestKind.FREEZE,
            sent_at=now - timedelta(days=30),
            due_at=now - timedelta(days=23),
            analyst="alice@lab",
            identified_by="env",
        )
    )
    ledger.close()
    return path


def _server(store_path, case_path, **kw):
    from chainscope.agent.server import ServerConfig, build_server

    return build_server(ServerConfig(store=store_path, case=case_path, **kw))


class TestCaseRecord:
    def test_open_questions_and_unanswered_requests_come_back_together(
        self, store_path, case_path
    ):
        # An agent asked to summarise a case has to see both, or it will report
        # a case as finished when it is waiting on an exchange.
        out = _call(_server(store_path, case_path), "case_record", {})
        assert [q["asked"] for q in out["open_questions"]] == [
            "who funded the gas on the first payout?"
        ]
        assert out["awaiting_reply"][0]["counterparty"] == "Binance"
        assert out["awaiting_reply"][0]["overdue"] is True

    def test_it_warns_against_reading_silence_as_refusal(self, store_path, case_path):
        out = _call(_server(store_path, case_path), "case_record", {})
        assert "not a request that was refused" in out["reading_this"]

    def test_an_empty_case_says_nobody_wrote_anything(self, store_path, tmp_path):
        out = _call(_server(store_path, tmp_path / "empty.db"), "case_record", {})
        assert "nobody has written anything down" in out["note"]

    def test_superseded_notes_are_marked_not_dropped(self, store_path, case_path):
        from datetime import datetime, timezone

        from chainscope.case.log import CaseLog, Note, NoteKind

        log = CaseLog(case_path)
        log.add(
            Note(
                at=datetime.now(timezone.utc),
                analyst="alice@lab",
                identified_by="env",
                kind=NoteKind.CORRECTION,
                body="nine, not eleven",
                supersedes=2,
            )
        )
        log.close()

        out = _call(_server(store_path, case_path), "case_record", {})
        by_id = {n["id"]: n for n in out["notes"]}
        assert by_id[2]["superseded"] is True
        assert by_id[2]["body"] == "eleven fresh addresses"


class TestRecordNote:
    def test_it_is_off_without_writable(self, store_path, case_path):
        # The tool is not registered at all without --writable, so the SDK
        # refuses it as unknown. Asserting *that* rather than "either of two
        # messages" is the difference between checking the gate and checking
        # that something, anything, went wrong.
        message = _raises(
            _server(store_path, case_path),
            "record_note",
            {"kind": "observation", "body": "x"},
        )
        assert "Unknown tool" in message and "record_note" in message

    def test_and_the_read_side_still_works_without_writable(self, store_path, case_path):
        # The gate must not take the whole case surface with it: reading what
        # is unresolved is not a write and an agent needs it to summarise.
        out = _call(_server(store_path, case_path), "case_record", {})
        assert "open_questions" in out

    def test_an_agent_note_is_marked_as_one(self, store_path, case_path):
        # Same guarantee as label_address: a human reading the narrative later
        # must be able to tell a model's reasoning from their own.
        from chainscope.case.log import CaseLog

        server = _server(store_path, case_path, writable=True, agent_name="demo")
        _call(
            server,
            "record_note",
            {"kind": "decision", "body": "stopping at the CEX deposit"},
        )
        log = CaseLog(case_path)
        try:
            written = [n for n in log.notes() if n.body == "stopping at the CEX deposit"]
        finally:
            log.close()
        assert written[0].analyst == "agent:demo"
        assert written[0].identified_by == "agent"

    def test_a_correction_without_a_target_is_refused(self, store_path, case_path):
        server = _server(store_path, case_path, writable=True)
        message = _raises(
            server, "record_note", {"kind": "correction", "body": "that was wrong"}
        )
        assert "name the note it replaces" in message

    def test_an_unknown_kind_is_refused(self, store_path, case_path):
        server = _server(store_path, case_path, writable=True)
        message = _raises(server, "record_note", {"kind": "hunch", "body": "maybe"})
        assert "hunch" in message
