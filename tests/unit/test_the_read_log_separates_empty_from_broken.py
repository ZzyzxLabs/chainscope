"""An empty canvas has five possible causes and the log names which.

The page draws counts. "22 addresses, 29 flows" is the same sentence whether
the walk saw everything or a provider refused a third of it, and a graph that
came back empty looks the same for an address with no history, a refused
request, a rate limit halfway through, an exhausted page budget, and an
endpoint that quietly served page one again.

`_fetch_page` is the only place a provider is read, so it is the only place
that can tell those apart. These tests hold the distinctions it records.

The `capped` case was found *by the log itself*, on the first case opened after
it existed: page eleven of a Lazarus address came back red next to nine pages
that had worked, and the message was Blockscout's documented ten-thousand-row
ceiling. A ceiling is the endpoint behaving correctly. What it means is that
the answer is a prefix --- narrow the window, or use a deeper provider --- and
that is a different instruction from "retry", which is what a failure implies.
"""

from __future__ import annotations

from typing import Any

import pytest

from chainscope.core.chainid import ChainId
from chainscope.providers.base import ProviderError, ResultTruncated
from chainscope.server import local
from chainscope.server.activity import Log, is_ceiling

CHAIN = ChainId.evm(1)
ADDRESS = "0x" + "ab" * 20


class _Provider:
    """Answers one way, whichever way the test asked for."""

    name = "stub"

    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def asset_transfers(self, *_: Any, **__: Any) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture(autouse=True)
def fresh_log(monkeypatch: pytest.MonkeyPatch) -> Log:
    log = Log()
    monkeypatch.setattr(local, "LOG", log)
    return log


def _read(answer: Any, page: int = 1) -> None:
    local._fetch_page(_Provider(answer), CHAIN, ADDRESS, page)


def outcomes(log: Log) -> list[str]:
    return [event["outcome"] for event in log.recent()]


def test_rows_are_ok(fresh_log: Log) -> None:
    _read([object()])
    assert outcomes(fresh_log) == ["ok"]
    assert fresh_log.recent()[0]["rows"] == 1


def test_no_rows_is_empty_not_a_failure(fresh_log: Log) -> None:
    """An address with no history is a result. It must not read as an outage."""
    _read([])
    assert outcomes(fresh_log) == ["empty"]


def test_a_full_page_says_there_is_more(fresh_log: Log) -> None:
    _read(ResultTruncated("full", rows=[object(), object()]))
    event = fresh_log.recent()[0]
    assert event["outcome"] == "more"
    assert event["rows"] == 2


def test_the_providers_own_not_found_is_empty(fresh_log: Log) -> None:
    _read(ProviderError("blockscout refused: No token transfers found"))
    assert outcomes(fresh_log) == ["empty"]


def test_a_refusal_is_a_failure_and_carries_its_reason(fresh_log: Log) -> None:
    with pytest.raises(ProviderError):
        _read(ProviderError("blockscout refused: 429 rate limited"))
    event = fresh_log.recent()[0]
    assert event["outcome"] == "failed"
    assert "429" in event["detail"]


def test_a_paging_ceiling_is_capped_not_failed(fresh_log: Log) -> None:
    """The distinction the log found on its own first real case."""
    with pytest.raises(ProviderError):
        _read(
            ProviderError(
                "blockscout refused: Result window is too large, "
                "PageNo x Offset size must be less than or equal to 10000"
            ),
            page=11,
        )
    event = fresh_log.recent()[0]
    assert event["outcome"] == "capped"
    assert event["what"] == "asset_transfers page 11"


def test_an_unexpected_error_is_still_recorded(fresh_log: Log) -> None:
    """A read that dies some other way still produced no rows."""
    with pytest.raises(ValueError):
        _read(ValueError("socket closed"))
    event = fresh_log.recent()[0]
    assert event["outcome"] == "failed"
    assert "ValueError" in event["detail"]


def test_a_ceiling_is_recognised_by_wording_not_by_provider() -> None:
    assert is_ceiling("Result window is too large, PageNo x Offset size must be")
    assert is_ceiling("Max offset exceeded")
    assert not is_ceiling("429 Too Many Requests")
    assert not is_ceiling("No token transfers found")


def test_the_summary_counts_everything_not_just_what_is_shown(fresh_log: Log) -> None:
    """Failures that scrolled off the end of the view are still failures."""
    for _ in range(3):
        _read([])
    with pytest.raises(ProviderError):
        _read(ProviderError("boom"))
    counts = fresh_log.summary()
    assert counts["empty"] == 3
    assert counts["failed"] == 1
    assert counts["total"] == 4
    assert len(fresh_log.recent(limit=1)) == 1


def test_the_log_is_bounded() -> None:
    """A long run must not grow memory without limit."""
    log = Log(capacity=5)
    for i in range(20):
        log.record(
            provider="p", chain="eip155:1", what=f"read {i}", address=ADDRESS, outcome="ok"
        )
    assert log.summary()["total"] == 5
    assert log.recent()[0]["what"] == "read 19"


def test_the_detail_cannot_grow_without_bound() -> None:
    """A provider returning an HTML error page must not fill every row."""
    log = Log()
    log.record(
        provider="p",
        chain="eip155:1",
        what="read",
        address=ADDRESS,
        outcome="failed",
        detail="x" * 5000,
    )
    assert len(log.recent()[0]["detail"]) <= 300
