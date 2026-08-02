"""The measurement the language choice rests on, re-run.

`docs/ROADMAP.md` says Python stays, and the argument is not a preference: at
200,000 transfers the DuckDB view build took **151 seconds** while pure-Python
taint ran at 594,000 transfers a second. The slow step was not compute --- it
was `executemany` issuing one prepared execution per row --- and staging through
CSV and `COPY` took it to 178,000 rows a second, 122x. A rewrite in another
language would have made an 0.08-second step faster while a 151-second step ran
beside it.

That is a load-bearing claim and nothing re-ran it. A documented number nobody
checks is the same kind of stale assertion this codebase keeps finding in
docstrings --- written once, true once, relied on afterwards.

So: generous bounds, an order of magnitude clear of the measured values, on a
tenth of the volume. This is not a benchmark. It fails when a change makes
something *categorically* slower --- a per-row round trip creeping back in --- and
stays quiet about the ordinary variation between machines.
"""

from __future__ import annotations

import time

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

N = 20_000


@pytest.fixture(scope="module")
def transfers() -> list[Transfer]:
    addresses = [Address(ETHEREUM, f"0x{i:040x}", f"0x{i:040x}") for i in range(500)]
    return [
        Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, f"0x{i:064x}"),
            sender=addresses[i % 500],
            recipient=addresses[(i + 1) % 500],
            amount=Amount(10**18, 18, "ETH"),
            kind=TransferKind.NATIVE,
            block=i,
        )
        for i in range(N)
    ]


class TestTheWritePathStaysBulk:
    def test_writing_is_not_row_by_row(self, transfers, tmp_path) -> None:
        """Measured at ~117,000 rows/s. The bound is 10,000.

        A per-row round trip lands around 1,600/s --- that is what this catches,
        and the gap is wide enough that no ordinary machine difference reaches
        it.
        """
        store = SqliteStore(tmp_path / "bench.db")
        try:
            start = time.perf_counter()
            store.put_transfers(transfers)
            elapsed = time.perf_counter() - start
        finally:
            store.close()
        assert N / elapsed > 10_000, f"{N / elapsed:,.0f} rows/s"

    def test_chain_aware_keying_did_not_cost_the_write_path(self) -> None:
        """`address_key` runs per address on every write.

        It was added to fix a correctness bug on three chains, and justified on
        the basis that the adapter lookup is cached. Measured: 2% of the write
        path, 5.9M calls a second. This pins the cache --- without it the lookup
        imports a module per call and the write path collapses.
        """
        from chainscope.chains import address_key

        address = "0x" + "a" * 40
        start = time.perf_counter()
        for _ in range(50_000):
            address_key(ETHEREUM, address)
        elapsed = time.perf_counter() - start
        assert 50_000 / elapsed > 500_000, f"{50_000 / elapsed:,.0f}/s"


class TestTheAnalyticsBuildStaysBulk:
    def test_the_duckdb_view_is_not_built_row_by_row(self, transfers, tmp_path) -> None:
        """The 151-second step, and the reason another language was not the fix.

        Measured after the CSV+COPY change at ~174,000 rows/s. The bound is
        20,000 --- an order of magnitude above `executemany`'s 1,460/s and well
        below anything a slow machine would produce.
        """
        duckdb = pytest.importorskip("duckdb")
        assert duckdb  # used via AnalyticsView
        from chainscope.store.analytics import AnalyticsView

        path = tmp_path / "store.db"
        store = SqliteStore(path)
        try:
            store.put_transfers(transfers)
        finally:
            store.close()

        view = AnalyticsView(":memory:")
        start = time.perf_counter()
        view.build_from_sqlite(path)
        elapsed = time.perf_counter() - start
        assert N / elapsed > 20_000, f"{N / elapsed:,.0f} rows/s"


class TestTaintStaysFastEnoughToNotMatter:
    def test_the_step_a_rewrite_would_have_optimised(self, transfers) -> None:
        """594,000 transfers a second, measured.

        Quoted here because it is the other half of the argument: the analysis
        code was never the bottleneck, so rewriting it would have been an
        expensive way to not fix the problem.
        """
        from chainscope.analysis.taint import trace_taint

        source = transfers[0].sender.key
        start = time.perf_counter()
        trace_taint(transfers, {source: 10**18})
        elapsed = time.perf_counter() - start
        assert N / elapsed > 50_000, f"{N / elapsed:,.0f} transfers/s"
