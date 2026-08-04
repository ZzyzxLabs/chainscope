"""Appearing in a case is not the same as having been looked at.

The graph endpoint decided whether to fetch an address by asking whether the
store held any edges for it. That is a different question and it answers this
one wrongly: an address that turns up only as somebody else's counterparty
*has* edges, so opening it never fetched its own history --- and the case then
showed the slice that leaked in from its neighbour as though it were the whole
thing.

Concretely, in the LpdFi case: `0xb92fe925…4fff4f` paid the address that staked
the attacker and received the proceeds back. It was in the store the moment its
neighbour was fetched, so asking where *its* money came from returned six
payments outward and nothing inward, for ever, without ever making a request.

`Store.mark_expanded` has existed since the beginning and documents exactly
this --- "an address that was seen but never followed looks identical to one
that was followed and had nothing" --- and was called from nowhere. These tests
hold the wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from chainscope.core.chainid import ChainId
from chainscope.server import local

CHAIN = ChainId.evm(56)
SEEN_ONLY = "0x" + "b9" * 20
FETCHED = "0x" + "5d" * 20


class _Store:
    """Records what was marked, and answers `edges` for everything."""

    def __init__(self) -> None:
        self.expanded: set[tuple[str, str]] = set()
        self.written: list[Any] = []

    def put_transfers(self, rows: Any, **_: Any) -> int:
        self.written.extend(rows)
        return len(self.written)

    def mark_expanded(self, address: str, chain: ChainId, *, depth: int = 0) -> None:
        self.expanded.add((address.lower(), str(chain)))

    def is_expanded(self, address: str, chain: ChainId) -> bool:
        return (address.lower(), str(chain)) in self.expanded

    def edges(self, *_: Any, **__: Any) -> list[Any]:
        # The old gate's question. Deliberately always "yes" --- that is the
        # situation the bug lived in.
        return [object()]

    def close(self) -> None:
        pass


class _Provider:
    name = "stub"

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls = 0

    def asset_transfers(self, *_: Any, **__: Any) -> list[Any]:
        self.calls += 1
        return self.rows


def transfer() -> Any:
    return SimpleNamespace(
        tx=SimpleNamespace(hash="0x" + "ab" * 32),
        index=0,
        block=1,
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    made = _Store()
    monkeypatch.setattr(local, "_asset_provider", lambda chain: [_Provider([transfer()])])
    return made


def test_a_fetch_records_that_the_address_was_followed(store: _Store) -> None:
    local._fetch_into(store, FETCHED, CHAIN)  # type: ignore[arg-type]
    assert store.is_expanded(FETCHED, CHAIN)


def test_an_address_that_was_never_fetched_is_not_marked(store: _Store) -> None:
    local._fetch_into(store, FETCHED, CHAIN)  # type: ignore[arg-type]
    assert not store.is_expanded(SEEN_ONLY, CHAIN)


def test_having_edges_does_not_mean_having_been_followed(store: _Store) -> None:
    """The whole bug in one line: the store answers `edges` for every address,
    and only one of them has actually been read."""
    local._fetch_into(store, FETCHED, CHAIN)  # type: ignore[arg-type]
    assert store.edges(SEEN_ONLY, CHAIN)
    assert not store.is_expanded(SEEN_ONLY, CHAIN)


def test_an_empty_result_still_counts_as_having_looked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "We looked and found nothing" is an answer that has to survive.
    Re-fetching an empty address on every open would spend a rate limit to
    rediscover it."""
    made = _Store()
    monkeypatch.setattr(local, "_asset_provider", lambda chain: [_Provider([])])
    written, _complete = local._fetch_into(made, FETCHED, CHAIN)  # type: ignore[arg-type]
    assert written == 0
    assert made.is_expanded(FETCHED, CHAIN)


def test_the_mark_is_per_chain(store: _Store) -> None:
    """The same address on two chains is two questions."""
    local._fetch_into(store, FETCHED, CHAIN)  # type: ignore[arg-type]
    assert not store.is_expanded(FETCHED, ChainId.evm(1))
