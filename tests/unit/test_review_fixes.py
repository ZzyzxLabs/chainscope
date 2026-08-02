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
