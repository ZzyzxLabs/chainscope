"""Paging goes parallel only once an address has proved it has more pages.

Paging is sequential by nature: you learn there is a page five by seeing that
page four came back full. Fetching a window speculatively therefore spends
requests on pages that may not exist --- and almost every address is one page.

So page one is fetched alone. The common case costs exactly one request, as it
did serially, and only an address that has already shown it has more pays for
the guess. That is the property these tests hold in place, because the
tempting simplification (fetch four pages immediately) is invisible in a test
that only checks the rows.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from chainscope.core.chainid import ETHEREUM
from chainscope.server import local


class FakeProvider:
    """Serves `pages` lists of rows, recording which pages were asked for."""

    def __init__(self, pages: list[list[object]], *, delay: float = 0.0):
        self._pages = pages
        self._delay = delay
        self.asked: list[int] = []
        self._lock = threading.Lock()

    def asset_transfers(self, chain, address, *, direction, limit, page):
        with self._lock:
            self.asked.append(page)
        if self._delay:
            time.sleep(self._delay)
        if page > len(self._pages):
            from chainscope.providers.base import ProviderError

            raise ProviderError("no token transfers found")
        return self._pages[page - 1]


def _rows(n: int, offset: int = 0) -> list[object]:
    return [
        SimpleNamespace(tx=SimpleNamespace(hash=f"0x{i + offset:064x}"), index=0)
        for i in range(n)
    ]


class _Store:
    def __init__(self) -> None:
        self.written: list[object] = []

    def put_transfers(self, rows) -> None:
        self.written.extend(rows)


def _run(monkeypatch, provider, **kw):
    # A list now: the fetcher tries providers in order, because the
    # best-declared one is not always a working one.
    monkeypatch.setattr(local, "_asset_provider", lambda chain: [provider])
    return local._fetch_into(_Store(), "0xabc", ETHEREUM, **kw)


def test_a_one_page_address_costs_exactly_one_request(monkeypatch) -> None:
    """The common case must not pay for speculation."""
    provider = FakeProvider([_rows(12)])
    written, complete = _run(monkeypatch, provider)
    assert provider.asked == [1], f"asked for {provider.asked} when one page existed"
    assert (written, complete) == (12, True)


def test_it_widens_once_a_full_page_proves_there_is_more(monkeypatch) -> None:
    provider = FakeProvider([_rows(1000), _rows(1000, 1000), _rows(5, 2000)])
    written, complete = _run(monkeypatch, provider, width=4)
    assert provider.asked[0] == 1, "page one must be fetched alone"
    assert set(provider.asked) >= {1, 2, 3}
    assert written == 2005
    assert complete is True


def test_the_window_overlaps(monkeypatch) -> None:
    """Seven pages must cost three round trips, not seven.

    The ramp is 1, then 2, then 4, so seven pages is exactly three rounds. The
    bound is stated against what serial would cost rather than as a bare number,
    so the test says what it is protecting.
    """
    pages = [_rows(1000, i * 1000) for i in range(6)] + [_rows(3, 6000)]
    provider = FakeProvider(pages, delay=0.25)
    started = time.monotonic()
    _run(monkeypatch, provider, width=4)
    elapsed = time.monotonic() - started
    serial = len(pages) * 0.25
    assert elapsed < serial * 0.6, (
        f"took {elapsed:.2f}s against {serial:.2f}s serial — the window did not overlap"
    )


def test_a_shallow_address_wastes_at_most_one_request(monkeypatch) -> None:
    """The ramp exists for this case, and it is the common one.

    A fixed window of four made a two-page address issue five requests. Starting
    at two and doubling keeps the shallow end cheap while still reaching full
    width after one round.
    """
    provider = FakeProvider([_rows(1000), _rows(4, 1000)])
    _run(monkeypatch, provider, width=4)
    assert len(provider.asked) <= 3, f"issued {provider.asked} for a two-page address"


def test_rows_land_in_page_order_whatever_the_network_did(monkeypatch) -> None:
    """Completion order must not decide the stored sequence."""

    class Jittery(FakeProvider):
        def asset_transfers(self, chain, address, *, direction, limit, page):
            # Later pages return sooner, so completion order is reversed.
            time.sleep(max(0.0, 0.3 - 0.1 * page))
            return FakeProvider.asset_transfers(
                self, chain, address, direction=direction, limit=limit, page=page
            )

    provider = Jittery([_rows(1000), _rows(1000, 1000), _rows(2, 2000)])
    store = _Store()
    # A list now: the fetcher tries providers in order, because the
    # best-declared one is not always a working one.
    monkeypatch.setattr(local, "_asset_provider", lambda chain: [provider])
    local._fetch_into(store, "0xabc", ETHEREUM, width=4)
    hashes = [row.tx.hash for row in store.written]
    assert hashes == sorted(hashes), "rows were stored in whatever order arrived"


def test_the_page_budget_still_reports_an_incomplete_read(monkeypatch) -> None:
    """A prefix that does not announce itself is the failure this guards."""
    provider = FakeProvider([_rows(1000, i * 1000) for i in range(20)])
    _written, complete = _run(monkeypatch, provider, max_pages=6, width=4)
    assert complete is False, "stopped on the budget but reported a complete read"
    assert max(provider.asked) <= 6, "the window overran the page budget"
