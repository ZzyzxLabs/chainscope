"""Four defects a review pass found, each pinned where it broke.

All four were in code written this session, and three of them produced or
permitted a wrong answer rather than an error. That is the value of the second
pass: measurement checks whether a technique works, and a reader checks whether
it works on the inputs nobody thought to try.
"""

from __future__ import annotations

import contextlib
import csv as csvmod
import io
from typing import ClassVar

import pytest

from chainscope.analysis.taint import trace_taint
from chainscope.cli.commands.sql import _emit
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.analytics import AnalyticsView
from chainscope.store.sqlite import SqliteStore

ETH = 10**18
USDC = 10**6
THIEF = "0xthief"


def move(sender, recipient, raw, block, *, decimals=18, symbol="ETH", asset=None):
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


class TestTaintDoesNotCrossAssets:
    """Holdings were keyed by address alone, so one FIFO queue held ETH and
    USDC together and a clean USDC payment could draw taint from a dirty ETH
    lot. Measured before the fix: 1,000 USDC of clean money reported as
    stolen."""

    ROWS: ClassVar = (
        move(THIEF, "0xa", 10 * ETH, 1),
        move("0xclean", "0xa", 1000 * USDC, 2, decimals=6, symbol="USDC", asset="0xusdc"),
        move("0xa", "0xb", 1000 * USDC, 3, decimals=6, symbol="USDC", asset="0xusdc"),
    )

    def test_a_clean_token_payment_is_not_tainted_by_dirty_native(self):
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert "0xb" not in result.tainted

    def test_the_dirty_native_stays_where_it_is(self):
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert result.tainted["0xa"] == 10 * ETH

    def test_the_split_is_available_per_asset(self):
        """Summing across assets gives base units, not a value. Anything
        quoting a figure needs the split."""
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert result.by_asset[("0xa", "")] == 10 * ETH

    def test_same_asset_multi_hop_still_works(self):
        rows = [move(THIEF, "0xa", 10 * ETH, 1), move("0xa", "0xb", 10 * ETH, 2)]
        assert trace_taint(rows, {THIEF: 10 * ETH}).tainted["0xb"] == 10 * ETH

    def test_a_tainted_token_propagates_within_its_own_asset(self):
        """The guard must not make token taint untraceable."""
        rows = [
            move(THIEF, "0xa", 500 * USDC, 1, decimals=6, symbol="USDC", asset="0xusdc"),
            move("0xa", "0xb", 500 * USDC, 2, decimals=6, symbol="USDC", asset="0xusdc"),
        ]
        result = trace_taint(rows, {(THIEF, "0xusdc"): 500 * USDC})
        assert result.tainted["0xb"] == 500 * USDC


class TestTheInMemoryViewIsLockedDown:
    """`build_from_sqlite` needs external access to COPY, and for an in-memory
    view the connection has to be kept --- the data lives in it. It was kept
    permissive, and `config.view is None` is the *default* agent path, so
    arbitrary SQL could read local files through DuckDB table functions."""

    @pytest.fixture
    def store(self, tmp_path):
        SqliteStore(tmp_path / "s.db").close()
        return tmp_path / "s.db"

    @pytest.mark.parametrize("path", [":memory:", "view.duckdb"])
    def test_file_reads_are_refused_after_a_build(self, store, tmp_path, path):
        target = path if path == ":memory:" else str(tmp_path / path)
        view = AnalyticsView(target)
        try:
            view.build_from_sqlite(store)
            with pytest.raises(Exception, match=r"(?i)permission|not allowed|disabled"):
                view.connect().execute("SELECT * FROM read_csv('/etc/hosts')").fetchone()
        finally:
            view.close()

    @pytest.mark.parametrize("path", [":memory:", "view.duckdb"])
    def test_ordinary_queries_still_work(self, store, tmp_path, path):
        """The lock-down has to not break the thing it protects."""
        target = path if path == ":memory:" else str(tmp_path / path)
        view = AnalyticsView(target)
        try:
            view.build_from_sqlite(store)
            assert view.sql_limited("SELECT 1 AS n", limit=1) == [(1,)]
        finally:
            view.close()


class TestCsvOutputIsEscaped:
    """Joining on commas produces a file whose columns shift from the first
    label containing one. "Binance 14, hot" is a plausible nametag, and
    malformed CSV that parses into the wrong columns is worse than none."""

    ROWS: ClassVar = [("Binance 14, hot", 5), ('say "hi"', 1), ("two\nlines", 2)]

    def _emit_csv(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit(["label", "n"], self.ROWS, "csv")
        return buf.getvalue()

    def test_it_round_trips_through_a_csv_reader(self):
        rows = list(csvmod.reader(io.StringIO(self._emit_csv())))
        assert rows[0] == ["label", "n"]
        assert [r[0] for r in rows[1:]] == [r[0] for r in self.ROWS]

    def test_every_row_keeps_its_column_count(self):
        rows = list(csvmod.reader(io.StringIO(self._emit_csv())))
        assert {len(r) for r in rows} == {2}

    def test_a_comma_does_not_create_a_column(self):
        rows = list(csvmod.reader(io.StringIO(self._emit_csv())))
        assert rows[1][1] == "5"


class TestNativeAndInternalAreDifferentMovements:
    """`kind` was missing from the store's identity, so a native transfer and
    an internal one of the same value in the same transaction between the same
    pair collided and one was dropped. That is precisely how swap proceeds and
    withdrawal payouts appear --- the shape the ASSET_TRANSFERS capability
    exists to capture."""

    def _pair(self, kind):
        return Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, "0x" + "1" * 64),
            sender=Address(ETHEREUM, "0xa", "0xa"),
            recipient=Address(ETHEREUM, "0xb", "0xb"),
            amount=Amount(ETH, 18, "ETH"),
            kind=kind,
            block=1,
            index=0,
        )

    def test_both_survive(self, tmp_path):
        from chainscope.store.base import Query

        store = SqliteStore(tmp_path / "s.db")
        try:
            store.put_transfers(
                [self._pair(TransferKind.NATIVE), self._pair(TransferKind.INTERNAL)],
                source="t",
            )
            assert len(list(store.transfers(Query(chain=ETHEREUM)))) == 2
        finally:
            store.close()

    def test_deduplication_still_works_within_a_kind(self, tmp_path):
        from chainscope.store.base import Query

        store = SqliteStore(tmp_path / "s.db")
        try:
            store.put_transfers([self._pair(TransferKind.NATIVE)], source="t")
            store.put_transfers([self._pair(TransferKind.NATIVE)], source="t")
            assert len(list(store.transfers(Query(chain=ETHEREUM)))) == 1
        finally:
            store.close()


class TestBlockscoutDoesNotClaimTransactions:
    """Declaring a capability without implementing it makes the router select
    you and fail rather than choose something that works. Fixed in the Sui
    provider earlier in the session, then repeated here in a provider written
    afterwards."""

    def test_transaction_is_not_declared(self):
        from chainscope.providers.base import Capability
        from chainscope.providers.blockscout import BlockscoutProvider

        assert not BlockscoutProvider().capabilities.covers(Capability.TRANSACTION)

    def test_what_it_does_declare_is_implemented(self):
        from chainscope.providers.base import Capability
        from chainscope.providers.blockscout import BlockscoutProvider

        provider = BlockscoutProvider()
        for capability, method in (
            (Capability.ADDRESS_HISTORY, "address_history"),
            (Capability.ASSET_TRANSFERS, "asset_transfers"),
            (Capability.LOGS, "get_logs"),
        ):
            assert provider.capabilities.covers(capability)
            assert method in type(provider).__dict__


class TestAnonymitySetCountsThePoolNotTheBookkeeping:
    """Claimed withdrawals were excluded from the candidate count, so the set
    shrank as the walk progressed and the last deposit in a busy pool looked
    unopposed. Claiming is this function's bookkeeping; the pool does not know
    about it, and confidence has to describe the pool."""

    def _world(self):
        from chainscope.analysis.mixer import MixerEvent

        deposits = [
            MixerEvent(tx="d0", block=100, address="0xa"),
            MixerEvent(tx="d1", block=101, address="0xb"),
        ]
        withdrawals = [
            MixerEvent(tx=f"w{i}", block=110 + i, address=f"0xr{i}") for i in range(3)
        ]
        return deposits, withdrawals

    def test_both_deposits_see_the_same_competition(self):
        from chainscope.analysis.mixer import correlate_withdrawals

        deposits, withdrawals = self._world()
        sets = [m.anonymity_set for m in correlate_withdrawals(deposits, withdrawals).matches]
        assert sets == [3, 3]

    def test_confidence_follows_it_down(self):
        from chainscope.analysis.mixer import correlate_withdrawals
        from chainscope.core.attribution import Confidence

        deposits, withdrawals = self._world()
        for match in correlate_withdrawals(deposits, withdrawals).matches:
            assert match.confidence is Confidence.LOW

    def test_a_withdrawal_is_still_claimed_only_once(self):
        """Counting the pool honestly must not start assigning one withdrawal
        to two deposits."""
        from chainscope.analysis.mixer import correlate_withdrawals

        deposits, withdrawals = self._world()
        result = correlate_withdrawals(deposits, withdrawals)
        assert len({m.withdrawal.tx for m in result.matches}) == len(result.matches)


class TestTaintSurvivesAnOverspend:
    """An address that received ten tainted ETH and paid out eleven --- which is
    ordinary, since it had a balance before the window --- passed *zero* taint
    downstream and kept all ten forever. The trace answered "the money stopped
    here", which is not incomplete but backwards."""

    ROWS: ClassVar = (
        move(THIEF, "0xa", 10 * ETH, 1),
        move("0xa", "0xb", 11 * ETH, 2),
    )

    def test_the_taint_moves_on(self):
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert result.tainted.get("0xb") == 10 * ETH

    def test_it_does_not_stay_parked(self):
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert "0xa" not in result.tainted

    def test_the_shortfall_is_still_reported(self):
        """The extra ETH came from outside the window and is not counted as
        clean --- that would be a claim about money nobody watched arrive."""
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert result.unresolved

    def test_conservation_still_holds(self):
        result = trace_taint(list(self.ROWS), {THIEF: 10 * ETH})
        assert result.total <= 10 * ETH

    def test_an_ordinary_chain_is_unaffected(self):
        rows = [
            move(THIEF, "0xa", 10 * ETH, 1),
            move("0xa", "0xb", 10 * ETH, 2),
            move("0xb", "0xc", 10 * ETH, 3),
        ]
        assert trace_taint(rows, {THIEF: 10 * ETH}).tainted["0xc"] == 10 * ETH


class TestBlockscoutHonoursWhatItIsGiven:
    """Two gaps in a provider written this session, both of the same shape:
    accepting an input and not using it."""

    def _provider(self, reply):
        from chainscope.providers.blockscout import BlockscoutProvider

        class Stub(BlockscoutProvider):
            captured: ClassVar[dict] = {}

            def _get(self, module, action, **params):
                Stub.captured = params
                return reply

        return Stub()

    def test_a_full_page_of_logs_is_refused_not_returned(self):
        """An enumeration is a set, so a missing element does not look like
        anything. A provider that can tell you it capped, should."""
        from chainscope.providers.base import ResultTruncated
        from chainscope.providers.blockscout import LOGS_PAGE

        provider = self._provider([{"topics": [], "data": "0x"}] * LOGS_PAGE)
        with pytest.raises(ResultTruncated, match="page size"):
            provider.get_logs(ETHEREUM, address="0xa")

    def test_a_short_page_of_logs_is_an_answer(self):
        provider = self._provider([{"topics": [], "data": "0x"}] * 3)
        assert len(provider.get_logs(ETHEREUM, address="0xa")) == 3

    def test_the_token_query_passes_its_block_range_upstream(self):
        """Accepting a range and dropping it returns the whole history under a
        caller's belief that it was narrowed --- and movements then get
        attributed to a window they never happened in."""
        provider = self._provider([])
        provider.asset_transfers(ETHEREUM, "0xa", start_block=100, end_block=200)
        assert type(provider).captured["startblock"] == 100
        assert type(provider).captured["endblock"] == 200

    def test_latest_becomes_the_end_of_chain_not_a_string(self):
        provider = self._provider([])
        provider.asset_transfers(ETHEREUM, "0xa", start_block=5)
        assert type(provider).captured["endblock"] == 999_999_999


class TestSuiTransactionTransfersAreIndexed:
    """`asset_transfers` numbered its transfers and `get_transaction` did not,
    so two methods in one provider disagreed about what `index` means. Not
    currently a loss --- the store's identity key includes `asset` --- but that
    is the store covering for the provider."""

    def _tx(self):
        from chainscope.chains.sui import SUI_MAINNET
        from chainscope.providers.sui import SuiProvider

        sender, recipient = "0x" + "a" * 64, "0x" + "b" * 64
        body = {
            "digest": "0x" + "1" * 64,
            "checkpoint": "42",
            "timestampMs": "1700000000000",
            "transaction": {"data": {"sender": sender}},
            "effects": {
                "status": {"status": "success"},
                "gasUsed": {
                    "computationCost": "0",
                    "storageCost": "0",
                    "storageRebate": "0",
                },
            },
            "balanceChanges": [
                {
                    "owner": {"AddressOwner": sender},
                    "coinType": "0x2::sui::SUI",
                    "amount": "-1000",
                },
                {
                    "owner": {"AddressOwner": recipient},
                    "coinType": "0x2::sui::SUI",
                    "amount": "1000",
                },
                {
                    "owner": {"AddressOwner": sender},
                    "coinType": "0x9::usdc::USDC",
                    "amount": "-1000",
                },
                {
                    "owner": {"AddressOwner": recipient},
                    "coinType": "0x9::usdc::USDC",
                    "amount": "1000",
                },
            ],
        }

        class Stub(SuiProvider):
            def _rpc(self, *a, **k):
                return body

        return Stub().get_transaction(SUI_MAINNET, "0x" + "1" * 64)

    def test_each_transfer_gets_a_distinct_index(self):
        indices = [t.index for t in self._tx().transfers]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices)

    def test_both_movements_survive(self):
        assert len(self._tx().transfers) == 2


class TestResultsCarryEnoughToRerun:
    """`Result.params` is documented as carrying the parameters that produced
    it --- that is the reproducibility claim. Several analyzers recorded the
    address and dropped the block range, the row cap, and the thresholds, so a
    saved result asserted it could be re-run and could not."""

    def test_every_analyzer_that_takes_a_window_uses_it(self):
        """Checked against the function body rather than a literal string.

        The first version of this test looked for `"start_block"` in the source
        and failed on `consolidation`, which records it through a helper ---
        an assertion written against an assumed shape, which is the mistake
        this file is otherwise about. What matters is that an accepted
        parameter is referenced somewhere after the signature, not how.
        """
        import ast
        import inspect

        from chainscope.cli.commands.analyze import available

        for name, cls in sorted(available().items()):
            taken = inspect.signature(cls.run).parameters
            body = ast.parse(inspect.getsource(cls.run).lstrip()).body[0]
            assert isinstance(body, ast.FunctionDef)
            used = {
                node.id
                for node in ast.walk(ast.Module(body=body.body, type_ignores=[]))
                if isinstance(node, ast.Name)
            }
            for arg in ("start_block", "end_block"):
                if arg in taken:
                    assert arg in used, (
                        f"{name} accepts {arg} and never references it; a saved "
                        f"result would claim to be reproducible and would not be"
                    )

    def test_taint_records_which_addresses_it_walked(self):
        """Its walk is capped, so two runs can cover different ground. Without
        the list, results from different providers are not comparable and
        nothing says so."""
        import inspect

        from chainscope.analysis.taint import TaintAnalyzer

        assert '"addresses_walked"' in inspect.getsource(TaintAnalyzer.run)


class TestThirdPartyActionsArePinned:
    """A tag is a mutable pointer. Whoever controls the repository can move v31
    to different code, and that code runs with this workflow's token."""

    def _workflow(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        return (root / ".github" / "workflows" / "ci.yml").read_text()

    def test_no_third_party_action_is_on_a_bare_tag(self):
        import re

        for line in self._workflow().splitlines():
            match = re.search(r"uses:\s*([\w.-]+)/([\w.-]+)@(\S+)", line)
            if not match or match.group(1) == "actions":
                continue
            ref = match.group(3)
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{match.group(1)}/{match.group(2)} is pinned to {ref!r}, which can be moved"
            )

    def test_each_pin_says_which_version_it_was(self):
        """A bare SHA is unreviewable. The comment is how anyone tells whether
        it is current."""
        import re

        for line in self._workflow().splitlines():
            if re.search(r"uses:\s*(?!actions/)[\w.-]+/[\w.-]+@[0-9a-f]{40}", line):
                assert "#" in line
