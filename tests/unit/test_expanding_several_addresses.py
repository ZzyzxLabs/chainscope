"""Expanding several addresses at once, without merging their outcomes.

The fetches are independent network waits, so they run together. The risk is in
the reporting: a batch that returns one number has thrown away which address it
belongs to, and "9 transfers fetched" across ten addresses says nothing about
whether nine of them failed.

So every address carries its own row, and a failure is named against the
address it happened to. One address failing is a fact about that address;
letting it take down the other nine turns a partial answer into no answer,
which is the opposite of what a batch is for.
"""

from __future__ import annotations

import threading
import time

import pytest

from chainscope.providers.base import ProviderError
from chainscope.server import local


class Fetches:
    """Records which addresses were fetched, and can fail for chosen ones."""

    def __init__(self, *, delay: float = 0.0, broken: set[str] | None = None):
        self.delay = delay
        self.broken = broken or set()
        self.seen: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, store, address, chain, **kw):
        with self.lock:
            self.seen.append(address)
        if self.delay:
            time.sleep(self.delay)
        if address in self.broken:
            raise ProviderError("upstream said no")
        return 3, True


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    from chainscope.server.local import ServerOptions, _Handlers

    options = ServerOptions(store=tmp_path / "store.db", writable=False)
    made = _Handlers(options=options)

    class Store:
        def edges(self, address, chain, *, direction):
            return []

        def close(self):
            pass

    monkeypatch.setattr(made, "_store", lambda: Store())
    monkeypatch.setattr(local, "_counterparties", lambda store, key, chain: [])
    return made


def _query(addresses: list[str], **extra):
    return {"address": addresses, "chain": ["1"], **{k: [v] for k, v in extra.items()}}


A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def test_a_single_address_keeps_its_shape(handlers, monkeypatch) -> None:
    """Existing callers and the documented reply must not break."""
    monkeypatch.setattr(local, "_fetch_into", Fetches())
    reply = handlers.expand(_query([A]))
    assert reply["address"] == A
    assert reply["fetched"] == 3
    assert reply["addresses"] == [A]


def test_comma_separated_and_repeated_both_work(handlers, monkeypatch) -> None:
    """A URL built by hand and one built by a page use different forms."""
    monkeypatch.setattr(local, "_fetch_into", Fetches())
    joined = handlers.expand(_query([f"{A},{B}"]))
    repeated = handlers.expand(_query([A, B]))
    assert joined["addresses"] == repeated["addresses"] == [A, B]


def test_the_same_address_twice_is_fetched_once(handlers, monkeypatch) -> None:
    fetches = Fetches()
    monkeypatch.setattr(local, "_fetch_into", fetches)
    reply = handlers.expand(_query([A, A, A.upper()]))
    assert reply["addresses"] == [A]
    assert fetches.seen == [A]


def test_one_failure_does_not_take_down_the_others(handlers, monkeypatch) -> None:
    """The whole reason a batch is worth having."""
    monkeypatch.setattr(local, "_fetch_into", Fetches(broken={B}))
    reply = handlers.expand(_query([A, B, C]))
    assert reply["failed"] == [B]
    rows = {row["address"]: row for row in reply["per_address"]}
    assert rows[A]["fetched"] == 3 and rows[C]["fetched"] == 3
    assert rows[B]["fetched"] == 0
    assert "ProviderError" in rows[B]["failed"]
    # A failed address is not a complete one.
    assert rows[B]["complete"] is False
    assert reply["complete"] is False


def test_a_failure_is_named_against_its_own_address(handlers, monkeypatch) -> None:
    """A bare total would read as every address having nothing to show."""
    monkeypatch.setattr(local, "_fetch_into", Fetches(broken={A, B, C}))
    reply = handlers.expand(_query([A, B, C]))
    assert reply["failed"] == [A, B, C]
    assert reply["fetched"] == 0
    for row in reply["per_address"]:
        assert row["failed"], f"{row['address']} failed but says nothing"


def test_the_fetches_overlap(handlers, monkeypatch) -> None:
    """Three 0.25s fetches must not cost 0.75s."""
    monkeypatch.setattr(local, "_fetch_into", Fetches(delay=0.25))
    started = time.monotonic()
    handlers.expand(_query([A, B, C]))
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"took {elapsed:.2f}s — the fetches ran in sequence"


def test_order_given_is_order_returned(handlers, monkeypatch) -> None:
    """So the reply can be read against the request."""
    monkeypatch.setattr(local, "_fetch_into", Fetches(delay=0.05))
    reply = handlers.expand(_query([C, A, B]))
    assert [row["address"] for row in reply["per_address"]] == [C, A, B]


def test_an_empty_request_is_refused(handlers) -> None:
    with pytest.raises(ValueError, match="address is required"):
        handlers.expand({"chain": ["1"]})
