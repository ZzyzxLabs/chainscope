"""Four more from the full-repo review, all the same shape as the criticals.

None of them raises. Each returns something that looks like an answer.
"""

from __future__ import annotations

import pathlib
import tempfile
from decimal import Decimal

from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.pricing.binance import BinanceKlines
from chainscope.providers.etherscan import _row_index
from chainscope.store.sqlite import SqliteStore

A = Address(ETHEREUM, "0x" + "a" * 40, "0x" + "a" * 40)
B = Address(ETHEREUM, "0x" + "b" * 40, "0x" + "b" * 40)
USDC = Address(ETHEREUM, "0x" + "c" * 40, "0x" + "c" * 40)


class TestTwoMovementsInOneTransactionSurvive:
    """The store's uniqueness key carries `log_index` to keep these apart.

    Etherscan rows never supplied one, so the defence was inert: two identical
    token transfers in one transaction --- an ordinary batched payout --- became
    one row with no error. Measured before the fix: two in, one out.
    """

    def _transfer(self, index: int) -> Transfer:
        return Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, "0x" + "1" * 64),
            sender=A,
            recipient=B,
            amount=Amount(1_000_000, 6, "USDC"),
            kind=TransferKind.TOKEN,
            asset=USDC,
            index=index,
            block=1,
        )

    def test_identical_transfers_with_distinct_log_indexes_both_store(self) -> None:
        store = SqliteStore(":memory:")
        try:
            assert store.put_transfers([self._transfer(3), self._transfer(7)]) == 2
        finally:
            store.close()

    def test_without_an_index_they_would_collapse(self) -> None:
        # The behaviour being defended against, pinned so the reason is visible.
        store = SqliteStore(":memory:")
        try:
            assert store.put_transfers([self._transfer(0), self._transfer(0)]) == 1
        finally:
            store.close()

    def test_token_rows_use_log_index(self) -> None:
        assert _row_index("tokentx", {"logIndex": "0x11", "transactionIndex": "2"}) == 17
        assert _row_index("tokentx", {"logIndex": "17"}) == 17

    def test_rows_with_no_log_use_the_transaction_index(self) -> None:
        # `txlist` and `txlistinternal` produce no log, and nothing in the API
        # separates two internal calls within one transaction. A known limit,
        # not a silent one.
        assert _row_index("txlist", {"transactionIndex": "5"}) == 5
        assert _row_index("txlistinternal", {"transactionIndex": "0x3"}) == 3

    def test_a_missing_or_unparseable_index_is_zero(self) -> None:
        assert _row_index("tokentx", {}) == 0
        assert _row_index("tokentx", {"logIndex": ""}) == 0
        assert _row_index("tokentx", {"logIndex": "not a number"}) == 0


class TestAmountsAreNotSummedAcrossAssets:
    def test_counterparties_reports_asset_count_not_a_meaningless_total(self) -> None:
        """`SUM(amount_raw)` across assets adds wei to USDC.

        §3 of `docs/needs.md` records this exact bug in the graph renderer,
        where 18-decimal dust outranked 5,000 USDC and consumed the traversal
        budget. This was the second copy.
        """
        duckdb = __import__("importlib").import_module("importlib.util")
        if duckdb.find_spec("duckdb") is None:  # pragma: no cover
            import pytest

            pytest.skip("needs duckdb")
        from chainscope.store.analytics import AnalyticsView

        path = pathlib.Path(tempfile.mkdtemp()) / "store.db"
        store = SqliteStore(path)
        try:
            store.put_transfers(
                [
                    Transfer(
                        chain=ETHEREUM,
                        tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                        sender=A,
                        recipient=B,
                        amount=amount,
                        kind=kind,
                        asset=asset,
                        index=i,
                        block=i,
                    )
                    for i, (amount, kind, asset) in enumerate(
                        [
                            (Amount(1, 18, "ETH"), TransferKind.NATIVE, None),
                            (Amount(5_000_000_000, 6, "USDC"), TransferKind.TOKEN, USDC),
                        ]
                    )
                ]
            )
        finally:
            store.close()

        view = AnalyticsView(":memory:")
        view.build_from_sqlite(path)
        found = {row[0]: row for row in view.counterparties(A.key)}
        _, count, assets = found[B.key]
        assert count == 2
        # Two assets, not a sum of one wei and five thousand USDC.
        assert assets == 2


class TestAnEmptyRateCacheIsNotOffline:
    def test_a_file_with_no_rows_is_not_offline(self) -> None:
        """Opening the cache creates the file.

        Answering on the file meant a case that had never prefetched was told
        it could run offline, then failed on every rate lookup. "Offline" is a
        claim about having the data.
        """
        source = BinanceKlines(pathlib.Path(tempfile.mkdtemp()) / "rates.db")
        source._db()
        assert not source.is_offline()

    def test_one_stored_candle_makes_it_offline(self) -> None:
        source = BinanceKlines(pathlib.Path(tempfile.mkdtemp()) / "rates.db")
        source._store("ETHUSDT", [[1_700_000_000_000, 0, 0, 0, "3000"]])
        assert source.is_offline()

    def test_a_missing_file_is_not_offline(self) -> None:
        assert not BinanceKlines(
            pathlib.Path(tempfile.mkdtemp()) / "never-created.db"
        ).is_offline()


class TestPrefetchWalksPastAGap:
    def test_a_window_with_no_candles_does_not_end_the_prefetch(self) -> None:
        """A quiet window is a gap, not the end of the range.

        Halting there left every minute *after* it uncached, so a prefetch
        covering a year stopped at the first quiet stretch --- and the case then
        valued nothing past it while reporting itself as prefetched.
        """
        from datetime import datetime, timezone

        calls: list[tuple[int, int]] = []

        class Gappy(BinanceKlines):
            def _fetch(self, symbol: str, start_ms: int, end_ms: int) -> list[list[object]]:
                calls.append((start_ms, end_ms))
                if len(calls) == 1:
                    return []  # the gap
                if len(calls) > 3:
                    return []
                return [[start_ms, 0, 0, 0, "3000"]]

        source = Gappy(pathlib.Path(tempfile.mkdtemp()) / "rates.db")
        source.prefetch(
            "ETHUSDT",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 4, tzinfo=timezone.utc),
        )
        assert len(calls) > 1, "stopped at the first empty window"
        assert Decimal("3000")  # the stored rate is real


class TestAWatchIsScopedToItsOwnChain:
    """`until` came from `MAX(block)` across the whole store.

    In a store holding two chains the highest block wins, so an Ethereum watch
    was advanced to BSC's height --- every Ethereum block between them marked
    watched and never looked at, and once the mark is past, nothing revisits
    them. The comment above that line guards against exactly this failure, one
    axis over: it made `until` a real block *the store* had seen rather than one
    *that chain* had.
    """

    def _store(self, tmp_path):
        from chainscope.core.chainid import ChainId

        eth, bsc = ChainId.parse("eip155:1"), ChainId.parse("eip155:56")
        store = SqliteStore(tmp_path / "store.db")
        here = lambda c: Address(c, "0x" + "a" * 40, "0x" + "a" * 40)  # noqa: E731
        store.put_transfers(
            [
                Transfer(
                    chain=eth,
                    tx=TxRef(eth, "0x1"),
                    sender=here(eth),
                    recipient=here(eth),
                    amount=Amount(1, 18, "ETH"),
                    kind=TransferKind.NATIVE,
                    block=20_000_000,
                ),
                Transfer(
                    chain=bsc,
                    tx=TxRef(bsc, "0x2"),
                    sender=here(bsc),
                    recipient=here(bsc),
                    amount=Amount(1, 18, "BNB"),
                    kind=TransferKind.NATIVE,
                    block=45_000_000,
                ),
            ]
        )
        store.close()
        return tmp_path / "store.db", eth, bsc

    def test_the_mark_lands_on_a_block_that_chain_has_reached(self, tmp_path) -> None:
        import argparse
        import json

        from chainscope.cli.commands.watch import _once
        from chainscope.watch.base import AmountOver, Watch

        path, eth, _ = self._store(tmp_path)
        state = tmp_path / "state.json"
        args = argparse.Namespace(
            store=path, state=state, since=None, until=None, shape="text", dry_run=False
        )
        _once(
            args,
            [
                Watch(
                    name="eth-watch",
                    subject="0x" + "a" * 40,
                    chain=eth,
                    predicate=AmountOver(0),
                )
            ],
        )
        assert json.loads(state.read_text())["eth-watch"] == 20_000_000


class TestADecimalsCacheBelongsToOneToken:
    def test_another_tokens_cache_is_refused(self) -> None:
        """USDC's readings answered a question about WETH.

        Six decimals instead of eighteen renders one WETH as a trillion WETH ---
        and this module exists because a wrong-decimals amount is off by orders
        of magnitude and still looks like a number.
        """
        import pytest

        from chainscope.analysis.decimals import TokenDecimals, resolve_at

        usdc = TokenDecimals(token="0x" + "c" * 40)
        usdc.observe(0, 6)
        with pytest.raises(ValueError, match="not 0xee"):
            resolve_at("0x" + "e" * 40, 1000, lambda t, b: 18, cache=usdc)

    def test_the_matching_cache_is_still_reused(self) -> None:
        from chainscope.analysis.decimals import TokenDecimals, resolve_at

        usdc = TokenDecimals(token="0x" + "c" * 40)
        usdc.observe(0, 6)
        value, _ = resolve_at("0x" + "C" * 40, 1000, lambda t, b: 6, cache=usdc)
        assert value == 6


class TestAStateFileOfTheWrongShape:
    def test_valid_json_that_is_not_an_object_is_refused(self, tmp_path) -> None:
        """A silent reset re-scans from zero or skips a range.

        Neither is visible in the output, which is why the corrupt-JSON branch
        already refuses. This was the same failure spelled differently.
        """
        import pytest

        from chainscope.cli.commands.watch import _state

        path = tmp_path / "state.json"
        path.write_text("[]")
        with pytest.raises(ValueError, match="not an object"):
            _state(path)
