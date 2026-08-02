"""The analytical view.

Two things here are worth guarding hardest.

**Exactness across the type boundary.** The store keeps amounts as zero-padded
text because SQLite's INTEGER is 64-bit and wei is not; the view keeps them as
DuckDB HUGEINT. Every transfer crosses that boundary on build, and a rounding
step anywhere in between would be invisible in small numbers and catastrophic
in real ones.

**The SQL surface.** It is meant to be reachable from a CLI, a notebook, and
eventually an agent. DuckDB's table functions take file paths, so a connection
handed to any of those unrestricted is a filesystem read primitive rather than
a query interface.
"""

import importlib.util

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.analytics import (
    AnalyticsError,
    AnalyticsView,
    UnsafeQuery,
    assert_read_only_sql,
)
from chainscope.store.sqlite import SqliteStore

# The analytical layer is an optional extra and the module imports fine without
# it --- duckdb is loaded lazily, so that a minimal install can still import
# and get a useful error rather than an ImportError from somewhere unrelated.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="needs chainscope[analytics]",
)

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40

# Ten ETH. The number that does not fit in a signed 64-bit integer, and the
# reason none of this is stored as INTEGER.
TEN_ETH = 10 * 10**18


def addr(raw: str, chain=ETHEREUM):
    return Address(chain, raw, raw.lower())


def transfer(
    sender, recipient, raw, *, symbol="ETH", decimals=18, chain=ETHEREUM, i=0, ts=None
):
    return Transfer(
        chain=chain,
        tx=TxRef(chain, f"0x{i:064x}"),
        sender=addr(sender, chain),
        recipient=addr(recipient, chain),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.NATIVE,
        block=18_000_000 + i,
        index=i,
    )


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "store.db"
    store = SqliteStore(path)
    store.put_transfers(
        [
            transfer(A, B, TEN_ETH, i=1),
            transfer(A, B, TEN_ETH, i=2),
            transfer(A, C, TEN_ETH * 3, i=3),
            transfer(B, C, 5_000_000, symbol="USDC", decimals=6, i=4),
            transfer(C, A, TEN_ETH, chain=BSC, symbol="BNB", i=5),
        ],
        source="test",
    )
    store.close()
    return path


@pytest.fixture
def view(store_path, tmp_path):
    v = AnalyticsView(tmp_path / "view.duckdb")
    v.build_from_sqlite(store_path)
    yield v
    v.close()


class TestBuild:
    def test_every_transfer_arrives(self, view):
        assert view.stats()["transfers"] == 5

    def test_build_reports_what_it_did(self, store_path, tmp_path):
        v = AnalyticsView(tmp_path / "v.duckdb")
        stats = v.build_from_sqlite(store_path)
        try:
            assert stats.transfers == 5
            assert stats.seconds > 0
            assert stats.source == str(store_path)
        finally:
            v.close()

    def test_rebuilding_is_idempotent(self, store_path, tmp_path):
        """It is a derived view; building twice must not double the rows."""
        v = AnalyticsView(tmp_path / "v.duckdb")
        try:
            v.build_from_sqlite(store_path)
            v.build_from_sqlite(store_path)
            assert v.stats()["transfers"] == 5
        finally:
            v.close()

    def test_a_missing_store_is_a_clear_error(self, tmp_path):
        v = AnalyticsView(":memory:")
        with pytest.raises(AnalyticsError, match="no store at"):
            v.build_from_sqlite(tmp_path / "nothing.db")

    def test_chains_are_kept_apart(self, view):
        assert view.stats()["chains"] == sorted([str(ETHEREUM), str(BSC)])

    def test_attributions_come_across(self, tmp_path):
        path = tmp_path / "s.db"
        store = SqliteStore(path)
        store.put_attributions(
            [
                Attribution(
                    label="Binance 14",
                    category=Category.CEX,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="etherscan",
                    address=A,
                    chain=ETHEREUM,
                )
            ]
        )
        store.close()
        v = AnalyticsView(":memory:")
        try:
            assert v.build_from_sqlite(path).attributions == 1
        finally:
            v.close()


class TestExactness:
    def test_sums_exceed_int64_and_stay_exact(self, view):
        """The whole reason this layer exists rather than SUM(CAST(...))."""
        totals = dict((s, t) for s, t, _ in view.totals_by_asset(A))
        assert totals["ETH"] == TEN_ETH * 5
        assert totals["ETH"] > 2**63 - 1

    def test_no_float_creeps_in(self, view):
        total = view.totals_by_asset(A)[0][1]
        assert isinstance(total, int)
        # A float64 would have lost the low-order digits by now.
        assert total % 10**18 == 0

    def test_engine_sum_matches_python_sum(self, view, store_path):
        """The two representations must agree, or the view is quietly wrong."""
        import sqlite3

        conn = sqlite3.connect(store_path)
        expected = sum(
            int(r[0])
            for r in conn.execute("SELECT amount_raw FROM transfers WHERE symbol = 'ETH'")
        )
        conn.close()
        got = view.sql("SELECT SUM(amount_raw) FROM transfers WHERE symbol = 'ETH'")[0][0]
        assert int(got) == expected

    def test_token_decimals_survive(self, view):
        """USDC is six decimals; conflating them with eighteen is a factor of
        a trillion."""
        rows = view.sql("SELECT decimals FROM transfers WHERE symbol = 'USDC'")
        assert [r[0] for r in rows] == [6]


class TestFlows:
    def test_flows_aggregate_by_counterparty(self, view):
        """Two transfers to one address are one edge, which is what makes a
        graph with four hundred transfers renderable."""
        flows = {f.recipient: f for f in view.flows(A)}
        assert flows[B].transfer_count == 2
        assert flows[B].total_raw == TEN_ETH * 2

    def test_flows_are_largest_first(self, view):
        totals = [f.total_raw for f in view.flows(A)]
        assert totals == sorted(totals, reverse=True)

    def test_direction_in(self, view):
        assert [f.sender for f in view.flows(C, direction="in")] == [A, B]

    def test_min_total_filters(self, view):
        assert view.flows(A, min_total=TEN_ETH * 100) == []

    def test_chain_scopes_the_result(self, view):
        assert view.flows(C, chain=BSC)[0].symbol == "BNB"
        assert view.flows(C, chain=ETHEREUM) == []

    def test_a_bad_direction_is_rejected(self, view):
        with pytest.raises(AnalyticsError, match="direction"):
            view.flows(A, direction="sideways")

    def test_counterparties_sees_both_directions(self, view):
        assert {c[0] for c in view.counterparties(C)} == {A, B}


class TestSqlSafety:
    @pytest.mark.parametrize(
        "query",
        [
            "COPY transfers TO '/tmp/leak.csv'",
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM read_parquet('/etc/shadow')",
            "ATTACH '/etc/passwd' AS x",
            "INSTALL httpfs",
            "DROP TABLE transfers",
            "INSERT INTO transfers VALUES (1)",
            "UPDATE transfers SET amount_raw = 0",
            "CREATE TABLE evil (x INT)",
        ],
    )
    def test_writes_and_file_access_are_refused(self, view, query):
        with pytest.raises(UnsafeQuery):
            view.sql(query)

    def test_chained_statements_are_refused(self, view):
        """Only the first would be checked otherwise."""
        with pytest.raises(UnsafeQuery, match="one statement at a time"):
            view.sql("SELECT 1; DROP TABLE transfers")

    def test_a_statement_hidden_behind_a_comment_is_still_seen(self, view):
        with pytest.raises(UnsafeQuery):
            view.sql("SELECT 1 -- harmless\n; ATTACH 'x' AS y")

    def test_a_forbidden_word_inside_a_string_is_fine(self, view):
        """Searching for the literal 'drop' is an ordinary thing to want."""
        assert view.sql("SELECT COUNT(*) FROM transfers WHERE symbol = 'drop'") == [(0,)]

    def test_a_forbidden_word_inside_a_longer_word_is_fine(self, view):
        assert view.sql("SELECT 'copyright' AS x") == [("copyright",)]

    def test_reads_still_work(self, view):
        assert view.sql("SELECT COUNT(*) FROM transfers")[0][0] == 5

    def test_cte_and_explain_are_allowed(self, view):
        assert view.sql("WITH t AS (SELECT 1 AS x) SELECT x FROM t") == [(1,)]
        assert view.sql("EXPLAIN SELECT 1")

    def test_empty_query_is_rejected(self):
        with pytest.raises(UnsafeQuery, match="empty"):
            assert_read_only_sql("   ")

    def test_external_access_is_off_at_the_connection(self, view):
        """The guard rail above is a message; this is the actual boundary.

        Even if a statement got past the check, DuckDB itself must refuse to
        touch the filesystem.
        """
        conn = view.connect()
        with pytest.raises(
            Exception, match=r"(?i)permission|disabled|not allowed|Invalid Input"
        ):
            conn.execute("SELECT * FROM read_csv('/etc/passwd')")

    def test_parameters_are_bound_not_interpolated(self, view):
        """An address arriving from an agent is untrusted text."""
        assert view.sql(
            "SELECT COUNT(*) FROM transfers WHERE sender = ?", ["'; DROP TABLE t--"]
        ) == [(0,)]

    def test_a_broken_query_reports_clearly(self, view):
        with pytest.raises(AnalyticsError, match="query failed"):
            view.sql("SELECT nosuchcolumn FROM transfers")
