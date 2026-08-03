"""Corroboration asks two providers in parallel, and changes nothing else.

Asking twice is the only way to catch a provider returning ``200 OK`` with a
short answer, and it doubled the wait on a path whose cost is entirely latency.
Running the two concurrently removes that, but only if two properties hold, and
both are the kind that fail silently:

**The same requests get made.** Candidates are ranked by cost tier, so a naive
fan-out would bill a paid provider to corroborate an answer two free ones had
already agreed on. The window tops up only when a launched provider fails,
which is exactly when the serial loop would have tried the next one.

**The answer does not depend on who was quick.** The merge keeps the first
sighting of each row, so completion order deciding the merge would mean the
same case replayed on a slower connection could produce a different result.
"""

from __future__ import annotations

import threading
import time

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.providers.base import Capability, Provider, ProviderError
from chainscope.providers.router import Router


class Fake(Provider):
    """A provider that records that it was called, and can be slow or broken."""

    def __init__(self, name: str, rows, *, delay: float = 0.0, fails: bool = False):
        self.name = name
        self._rows = rows
        self._delay = delay
        self._fails = fails
        self.calls = 0
        self.chains = (ETHEREUM,)
        self.capabilities = (Capability.ADDRESS_HISTORY,)
        self.cost_tier = 0

    def supports(self, chain, capability) -> bool:
        return capability == Capability.ADDRESS_HISTORY

    def history(self):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._fails:
            raise ProviderError("upstream said no")
        return list(self._rows)


def _router(*providers: Fake) -> Router:
    router = Router(providers)
    # Rank strictly by the order given, so "preference order" is unambiguous.
    router.candidates = lambda chain, capability: list(providers)  # type: ignore[assignment]
    return router


def _ask(router: Router):
    return router.corroborate(
        ETHEREUM,
        Capability.ADDRESS_HISTORY,
        lambda p: p.history(),
        key=lambda row: row,
    )


def test_it_stops_at_two_successes_and_bills_no_further() -> None:
    """A third, costlier provider must not be called when two agreed."""
    a, b, c = Fake("a", ["x"]), Fake("b", ["x"]), Fake("c", ["x"])
    found = _ask(_router(a, b, c))
    assert found.corroborated
    assert (a.calls, b.calls) == (1, 1)
    assert c.calls == 0, "a third provider was billed for an answer already corroborated"


def test_a_failure_promotes_the_next_provider() -> None:
    """Two *successes* is the target, not two attempts."""
    a, b, c = Fake("a", [], fails=True), Fake("b", ["x"]), Fake("c", ["x"])
    found = _ask(_router(a, b, c))
    assert found.corroborated
    assert set(found.sources) == {"b", "c"}
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)
    assert any("a:" in f for f in found.failures)


def test_the_two_actually_overlap() -> None:
    """Two 0.3s providers must finish in well under 0.6s."""
    a, b = Fake("a", ["x"], delay=0.3), Fake("b", ["x"], delay=0.3)
    started = time.monotonic()
    _ask(_router(a, b))
    elapsed = time.monotonic() - started
    assert elapsed < 0.45, f"took {elapsed:.2f}s — the two calls ran in sequence"


def test_the_result_does_not_depend_on_which_answered_first() -> None:
    """The merge keeps the first sighting; that must mean first by preference.

    Both providers return a row with the same key and different identity. The
    slower one is listed first, so completion-ordered merging would keep the
    other and the answer would flip with the network.
    """
    for slow_first in (True, False):
        preferred = Fake("preferred", ["from-preferred"], delay=0.25 if slow_first else 0.0)
        other = Fake("other", ["from-other"], delay=0.0 if slow_first else 0.25)
        found = Router.corroborate(
            _router(preferred, other),
            ETHEREUM,
            Capability.ADDRESS_HISTORY,
            lambda p: p.history(),
            # One key for both rows, so the merge has to choose.
            key=lambda row: "same-row",
        )
        assert found.rows == ["from-preferred"], (
            "the row kept depends on which provider was quicker"
        )
        assert found.sources == ("preferred", "other")


def test_every_provider_failing_still_raises_with_all_reasons() -> None:
    a, b = Fake("a", [], fails=True), Fake("b", [], fails=True)
    with pytest.raises(ProviderError) as caught:
        _ask(_router(a, b))
    assert "a:" in str(caught.value) and "b:" in str(caught.value)


def test_a_single_candidate_skips_the_pool() -> None:
    """Keeps the stack trace and the audit log identical to the serial path."""
    seen: list[str] = []
    main = threading.current_thread().name

    def watch(provider):
        seen.append(threading.current_thread().name)
        return provider.history()

    router = _router(Fake("only", ["x"]))
    router.corroborate(ETHEREUM, Capability.ADDRESS_HISTORY, watch, key=lambda r: r)
    assert seen == [main], "a lone provider was dispatched to a worker thread"


def test_disagreement_is_still_reported() -> None:
    """The whole reason for asking twice must survive the change."""
    a, b = Fake("a", ["x", "y"]), Fake("b", ["x"])
    found = _ask(_router(a, b))
    assert found.disagreed
    assert found.only_in["a"] == ["y"]
    assert not found.corroborated
