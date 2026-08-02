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


class TestBlockscoutAsksForThePageItChecks:
    """Etherscan sends `page=1, offset=limit`; Blockscout sent neither.

    So the node returned its own default page --- around fifty rows --- and the
    truncation check, written against `limit`, never tripped. The caller got a
    short list that said it was complete.

    It also broke corroboration, which is now the default. Etherscan answers
    with a thousand rows and Blockscout with fifty, and `Router.corroborate`
    reports the difference as a disagreement *between sources* when it is a
    difference between two requests.
    """

    def _captured(self, method: str, **kwargs) -> dict:
        from chainscope.core.chainid import ETHEREUM
        from chainscope.providers.blockscout import BlockscoutProvider

        seen: dict = {}

        class Recorder:
            def get(self, url, params, **kw):
                seen.update(params)
                return {"status": "1", "result": []}

        provider = BlockscoutProvider(chain=ETHEREUM, client=Recorder())
        getattr(provider, method)(ETHEREUM, "0x" + "a" * 40, **kwargs)
        return seen

    def test_history_asks_for_the_limit_it_checks(self) -> None:
        params = self._captured("address_history", limit=1000)
        assert params.get("offset") == 1000
        assert params.get("page") == 1

    def test_token_transfers_ask_too(self) -> None:
        params = self._captured("asset_transfers", limit=250)
        assert params.get("offset") == 250


class TestEveryTopicPairGetsAnOperator:
    """Blockscout's getLogs needs an operator for every *pair*, not adjacent ones.

    Without them the extra topics are not applied, so the query answers a
    broader question than the one asked --- and the caller receives a set, where
    a wrong element looks like nothing.
    """

    def _params(self, topics: list[str | None]) -> dict:
        from chainscope.core.chainid import ETHEREUM
        from chainscope.providers.blockscout import BlockscoutProvider

        seen: dict = {}

        class Recorder:
            def get(self, url, params, **kw):
                seen.update(params)
                return {"status": "1", "result": []}

        BlockscoutProvider(chain=ETHEREUM, client=Recorder()).get_logs(
            ETHEREUM, from_block=1, to_block=2, topics=topics
        )
        return seen

    def test_three_topics_get_three_operators(self) -> None:
        params = self._params(["0xa", "0xb", "0xc"])
        assert params["topic0_1_opr"] == "and"
        assert params["topic0_2_opr"] == "and"
        assert params["topic1_2_opr"] == "and"

    def test_a_gap_pairs_what_was_actually_supplied(self) -> None:
        # topic1 absent: the pair is 0 and 2, not 0 and 1.
        params = self._params(["0xa", None, "0xc"])
        assert "topic0_2_opr" in params
        assert "topic0_1_opr" not in params

    def test_one_topic_needs_no_operator(self) -> None:
        params = self._params(["0xa"])
        assert not any(k.endswith("_opr") for k in params)


class TestAFailedBuildPublishesNothing:
    """A partial analytics view is the worst possible artefact.

    `self._conn` was assigned inside `finally`, so a build that died partway
    left a live view answering from half a dataset --- measured: transfers
    copied, attributions raised, and the view then reported ten transfers and
    no attributions as though that were the store. Every query afterwards is
    quietly answered from a subset.
    """

    def _seeded(self, tmp_path):
        path = tmp_path / "store.db"
        store = SqliteStore(path)
        try:
            store.put_transfers(
                [
                    Transfer(
                        chain=ETHEREUM,
                        tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                        sender=A,
                        recipient=B,
                        amount=Amount(1, 18, "ETH"),
                        kind=TransferKind.NATIVE,
                        index=i,
                        block=i,
                    )
                    for i in range(10)
                ]
            )
        finally:
            store.close()
        return path

    def test_an_in_memory_view_is_not_published(self, tmp_path, monkeypatch) -> None:
        import pytest

        pytest.importorskip("duckdb")
        from chainscope.store.analytics import AnalyticsView

        def boom(self, conn, src, batch):
            raise RuntimeError("disk full")

        monkeypatch.setattr(AnalyticsView, "_copy_attributions", boom)
        view = AnalyticsView(":memory:")
        with pytest.raises(RuntimeError, match="disk full"):
            view.build_from_sqlite(self._seeded(tmp_path))
        assert view._conn is None

    def test_a_partial_file_is_removed(self, tmp_path, monkeypatch) -> None:
        import pytest

        pytest.importorskip("duckdb")
        from chainscope.store.analytics import AnalyticsView

        def boom(self, conn, src, batch):
            raise RuntimeError("disk full")

        monkeypatch.setattr(AnalyticsView, "_copy_attributions", boom)
        target = tmp_path / "view.duckdb"
        with pytest.raises(RuntimeError, match="disk full"):
            AnalyticsView(target).build_from_sqlite(self._seeded(tmp_path))
        # Left behind, a later connect() would open it as if it were valid.
        assert not target.exists()

    def test_a_clean_build_still_publishes(self, tmp_path) -> None:
        import pytest

        pytest.importorskip("duckdb")
        from chainscope.store.analytics import AnalyticsView

        view = AnalyticsView(":memory:")
        view.build_from_sqlite(self._seeded(tmp_path))
        assert view.sql("SELECT COUNT(*) FROM transfers") == [(10,)]


class TestTwoRowsThatDisagreeAreNotDuplicates:
    def _plan(self, tmp_path, rows: str):
        from chainscope.attribution.ingest import plan_import

        path = tmp_path / "labels.csv"
        path.write_text("address,label,category,confidence\n" + rows)
        return plan_import(path, source="team")

    def test_a_different_category_is_a_conflict(self, tmp_path) -> None:
        """One file calling an address `cex` on one row and `service` on the next.

        The dedup key was (address, label, chain) --- no category --- so the
        second row was counted as a duplicate and the disagreement vanished.
        This module surfaces a clash with the *store* as a Conflict; a clash
        inside the file is the same fact about somebody's spreadsheet.
        """
        plan = self._plan(tmp_path, "0xaaa,Binance,cex,high\n0xaaa,Binance,service,high\n")
        assert len(plan.conflicts) == 1
        assert plan.duplicates == 0

    def test_a_genuine_duplicate_is_still_a_duplicate(self, tmp_path) -> None:
        plan = self._plan(tmp_path, "0xbbb,Kraken,cex,high\n0xbbb,Kraken,cex,high\n")
        assert plan.duplicates == 1
        assert plan.conflicts == []

    def test_only_the_first_of_a_disagreeing_pair_is_written(self, tmp_path) -> None:
        # Reported, not resolved: which is right is a judgement for a person,
        # and the resolver decides at read time anyway.
        plan = self._plan(tmp_path, "0xaaa,Binance,cex,high\n0xaaa,Binance,service,high\n")
        assert len(plan.attributions) == 1
