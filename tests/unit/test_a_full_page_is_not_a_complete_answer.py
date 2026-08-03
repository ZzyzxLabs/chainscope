"""A full page smaller than the one requested is still truncation.

The check compared what came back against what was *asked for*. When an
endpoint serves a smaller page than the caller requested, a full page of the
endpoint's size is smaller than the request and therefore looked complete.

Measured on a real case: a request for 10,000 internal transfers returned
exactly 1,000. Counting from that gave 286 transfers totalling 128.4 ETH; the
real answer was 747 and 352.8 --- a third of the truth, with nothing in the
response to notice. This is the exact failure the class was written to make
impossible, arriving through the one door it left open.
"""

from __future__ import annotations

import pytest

from chainscope.providers.base import ResultTruncated
from chainscope.providers.etherscan import EtherscanProvider


class Fake(EtherscanProvider):
    """An endpoint that serves `page_size` rows however many are requested."""

    def __init__(self, page_size: int, available: int):
        self.page_size = page_size
        self.available = available
        self.name = "fake"

    def _get(self, chain, module, action, **params):  # type: ignore[override]
        served = min(self.page_size, params.get("offset", 0), self.available)
        return [{"i": i} for i in range(served)]


def _paged(provider, limit):
    return EtherscanProvider._paged(provider, None, "account", "txlistinternal", limit=limit)


def test_a_full_page_below_the_request_is_reported() -> None:
    """1,000 served against 10,000 asked is a page, not an answer."""
    with pytest.raises(ResultTruncated) as caught:
        _paged(Fake(page_size=1000, available=7466), limit=10000)
    assert "full page" in str(caught.value)
    # The rows survive, so a caller that wants to page can use them.
    assert len(caught.value.rows) == 1000


def test_exactly_the_number_requested_is_still_reported() -> None:
    """The case that already worked must keep working."""
    with pytest.raises(ResultTruncated):
        _paged(Fake(page_size=10000, available=50000), limit=1000)


def test_a_short_page_is_a_complete_answer() -> None:
    """466 of a 1,000-row page means the data ran out. No exception."""
    rows = _paged(Fake(page_size=1000, available=466), limit=10000)
    assert len(rows) == 466


def test_an_empty_result_is_not_truncation() -> None:
    assert _paged(Fake(page_size=1000, available=0), limit=10000) == []


@pytest.mark.parametrize("size", [100, 1000, 10000])
def test_every_page_size_the_api_serves_is_recognised(size: int) -> None:
    with pytest.raises(ResultTruncated):
        _paged(Fake(page_size=size, available=size * 5), limit=10000)
