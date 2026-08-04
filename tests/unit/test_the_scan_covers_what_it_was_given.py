"""A chunk must cover its whole assignment, and the span must adapt upward.

Two bugs, one after the other, both mine, both in the shape this package is
supposed to catch.

`_chunk` was handed a 40,000-block range and fetched a single `span`-wide
request from the start of it, then returned. The remaining 35,000 blocks were
never read and nothing downstream could tell: fewer logs is exactly what an
address with less activity looks like.

And the span started wide and narrowed on failure, which costs a failed request
per step. A failed request that is a timeout is indistinguishable from an unwell
host, so four concurrent chunks each halving once opened the circuit breaker on
an endpoint that was answering. Starting narrow and widening on *success* costs
nothing when it declines to widen.
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.core.chainid import ChainId
from chainscope.providers.base import ProviderError
from chainscope.providers.jsonrpc import (
    _SPAN_CEILING,
    _SPAN_FLOOR,
    _SPAN_START,
    JsonRpcProvider,
)

CHAIN = ChainId.evm(56)


class _Node:
    """Records every range asked for, and optionally refuses wide ones."""

    def __init__(self, max_span: int | None = None) -> None:
        self.ranges: list[tuple[int, int]] = []
        self.max_span = max_span

    def rpc(self, url: str, method: str, params: Any = None, **_: Any) -> Any:
        if method != "eth_getLogs":
            return "0x0"
        lo = int(params[0]["fromBlock"], 16)
        hi = int(params[0]["toBlock"], 16)
        if self.max_span is not None and (hi - lo + 1) > self.max_span:
            raise ProviderError("query returned more than 10000 results")
        self.ranges.append((lo, hi))
        return []


def provider(node: _Node) -> JsonRpcProvider:
    return JsonRpcProvider("https://node.example", CHAIN, client=node)  # type: ignore[arg-type]


def covered(ranges: list[tuple[int, int]]) -> set[int]:
    seen: set[int] = set()
    for lo, hi in ranges:
        seen.update(range(lo, hi + 1))
    return seen


def test_a_chunk_covers_every_block_it_was_given() -> None:
    """The gap bug. A 40,000-block assignment at a 5,000 span is eight
    requests, not one."""
    node = _Node()
    rpc = provider(node)
    rpc._chunk(CHAIN, 1_000_000, 1_039_999, ["0xtopic"], None)
    assert covered(node.ranges) == set(range(1_000_000, 1_040_000))


def test_no_range_is_requested_twice() -> None:
    node = _Node()
    provider(node)._chunk(CHAIN, 1_000_000, 1_039_999, ["0xtopic"], None)
    blocks = [b for lo, hi in node.ranges for b in range(lo, hi + 1)]
    assert len(blocks) == len(set(blocks))


def test_the_first_request_is_cautious() -> None:
    """Starting wide costs a failure to discover the limit; starting narrow
    costs nothing when the endpoint turns out to be generous."""
    node = _Node()
    provider(node)._chunk(CHAIN, 1_000_000, 1_039_999, ["0xtopic"], None)
    first_lo, first_hi = node.ranges[0]
    assert first_hi - first_lo + 1 == _SPAN_START


def test_the_span_widens_after_success() -> None:
    node = _Node()
    provider(node)._chunk(CHAIN, 1_000_000, 1_039_999, ["0xtopic"], None)
    widths = [hi - lo + 1 for lo, hi in node.ranges]
    assert widths[1] > widths[0], "a working endpoint should be asked for more"
    assert max(widths) <= _SPAN_CEILING


def test_the_span_narrows_on_refusal_and_stays_narrow() -> None:
    """Rediscovering the same limit on every chunk is what made narrowing
    expensive enough to trip the breaker."""
    node = _Node(max_span=2_000)
    rpc = provider(node)
    rpc._chunk(CHAIN, 1_000_000, 1_019_999, ["0xtopic"], None)
    assert covered(node.ranges) == set(range(1_000_000, 1_020_000))
    assert all(hi - lo + 1 <= 2_000 for lo, hi in node.ranges)
    assert rpc._span <= 2_000


def test_an_endpoint_that_refuses_everything_raises_rather_than_looping() -> None:
    node = _Node(max_span=1)
    with pytest.raises(ProviderError):
        provider(node)._chunk(CHAIN, 1_000_000, 1_019_999, ["0xtopic"], None)
    assert all(hi - lo + 1 >= _SPAN_FLOOR for lo, hi in node.ranges) or not node.ranges


def test_a_single_block_range_is_one_request() -> None:
    node = _Node()
    provider(node)._chunk(CHAIN, 1_000_000, 1_000_000, ["0xtopic"], None)
    assert node.ranges == [(1_000_000, 1_000_000)]


def test_widening_does_not_undo_narrowing() -> None:
    """Without a remembered cap the span oscillates: succeed at 1,250, double
    to 2,500, get refused, halve back --- a wasted failed request every other
    round, for ever. Counted here rather than asserted in prose."""
    node = _Node(max_span=2_000)
    rpc = provider(node)
    rpc._chunk(CHAIN, 1_000_000, 1_079_999, ["0xtopic"], None)
    assert rpc._span <= 2_000
    assert rpc._span_cap <= 2_000
    # Every request that was *served* is inside the limit, and the served
    # widths stop shrinking once the cap is known.
    widths = {hi - lo + 1 for lo, hi in node.ranges}
    assert max(widths) <= 2_000
