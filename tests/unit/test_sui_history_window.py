"""A Sui window nobody could reach is not a window that was empty.

`suix_queryTransactionBlocks` filters by address and returns newest-first; it
cannot filter by checkpoint. So `address_history(start_block=..., end_block=...)`
fetched `limit` rows from the tip and applied the range afterwards --- and an
address with more than `limit` transactions *newer* than the window returned
**nothing** for it.

Nothing reads as "no activity in that period". That is the failure §1 of
`docs/needs.md` is built around, reached by arithmetic rather than by a provider
lying. Found by a full-repo review.
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.core.chainid import ChainId
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.providers.base import ResultTruncated
from chainscope.providers.sui import SuiProvider

SUI = ChainId.parse("sui:mainnet")
ADDR = "0x" + "a" * 64


def provider(rows: int, *, oldest: int) -> SuiProvider:
    """A provider whose history is `rows` transactions ending at `oldest`."""
    p = SuiProvider("http://localhost", client=None, chain=SUI)

    def fake(chain: Any, address: str, **kw: Any) -> list[Transfer]:
        limit = int(kw.get("limit", 100))
        a = Address(SUI, ADDR, ADDR)
        # Newest first, exactly as the node returns them.
        return [
            Transfer(
                chain=SUI,
                tx=TxRef(SUI, f"0x{i:064x}"),
                sender=a,
                recipient=a,
                amount=Amount(1, 9, "SUI"),
                kind=TransferKind.NATIVE,
                block=oldest + rows - 1 - i,
            )
            for i in range(min(rows, limit))
        ]

    p.asset_transfers = fake  # type: ignore[method-assign]
    return p


class TestAnUnreachableWindow:
    def test_it_raises_rather_than_returning_an_empty_range(self) -> None:
        # 100_000 transactions, all newer than the window asked for. The walk
        # cannot reach checkpoint 5 within its budget.
        with pytest.raises(ResultTruncated, match="stopped at the budget"):
            provider(100_000, oldest=1_000).address_history(
                SUI, ADDR, start_block=5, end_block=10, limit=50
            )

    def test_the_message_says_how_far_it_got(self) -> None:
        # So the reader can decide whether to narrow the window or raise the
        # limit, rather than guessing at why the answer was empty.
        with pytest.raises(ResultTruncated, match="paged back to checkpoint"):
            provider(100_000, oldest=1_000).address_history(
                SUI, ADDR, start_block=5, end_block=10, limit=50
            )


class TestAReachableWindow:
    def test_a_window_within_the_budget_is_returned(self) -> None:
        found = provider(80, oldest=1_000).address_history(
            SUI, ADDR, start_block=1_000, end_block=1_010, limit=50
        )
        assert [t.block for t in found] == list(range(1_000, 1_011))

    def test_it_fetches_past_the_limit_to_reach_the_range(self) -> None:
        """The whole fix. With `limit` rows from the tip this window is empty.

        60 transactions from checkpoint 1000; asking for 1000-1002 with a limit
        of 10 would have fetched only the newest ten (1050-1059) and filtered
        them all away.
        """
        found = provider(60, oldest=1_000).address_history(
            SUI, ADDR, start_block=1_000, end_block=1_002, limit=10
        )
        assert [t.block for t in found] == [1_000, 1_001, 1_002]

    def test_an_unbounded_query_still_honours_the_limit(self) -> None:
        # No window asked for, so no walk: the budget must not silently turn
        # every history call into ten times the requested work.
        found = provider(500, oldest=1).address_history(SUI, ADDR, limit=25)
        assert len(found) == 25


class TestTheTipIsNotCachedAsSettled:
    def test_the_first_page_is_short_lived(self) -> None:
        """An address's history is a changing aggregate.

        Cached for thirty days, a second look a week later returns the same
        answer and silently misses everything since.
        """
        import inspect

        from chainscope.transport.cache import Volatility

        source = inspect.getsource(SuiProvider._query_blocks)
        assert "Volatility.SLOW if cursor is None" in source
        assert Volatility.SLOW is not Volatility.SETTLED
