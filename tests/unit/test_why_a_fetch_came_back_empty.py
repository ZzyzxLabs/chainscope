"""Zero rows has several causes and only one of them is about the address.

The graph endpoint used to answer a failed fetch with one fixed sentence::

    0x5d28… has no transfers on eip155:56 --- not in the store, and the
    providers returned none either. That is an answer about the address
    rather than about the store

The last clause is a claim, and it was made unconditionally. It is true only
when a source that can see *everything* read the *whole* history and found
nothing. Zero also comes back when the serving provider reads ERC-20 logs and
therefore cannot see a native send, when the scan covered a window rather than
all of history, and when a provider stopped at its paging ceiling.

Those are facts about the looking. Asserting them as facts about the address is
the confusion this package exists to prevent, stated in the tool's own voice ---
where a reader has no way to check it. The address in that message was a live
exploiter holding 689,429.79 USDC.

`_nothing_found` builds the sentence from the read log instead, so what it says
is a description of what happened rather than a constant.
"""

from __future__ import annotations

import pytest

from chainscope.core.chainid import ChainId
from chainscope.server import local
from chainscope.server.activity import Log

CHAIN = ChainId.evm(56)
ADDRESS = "0x5d289266d85ef671561ba3f253fb79327c193f33"

#: The one clause that must appear only when it is earned.
ABOUT_THE_ADDRESS = "an answer about the address"


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Log:
    fresh = Log()
    monkeypatch.setattr(local, "LOG", fresh)
    return fresh


def _read(log: Log, what: str, outcome: str, detail: str = "") -> None:
    log.record(
        provider="stub",
        chain=str(CHAIN),
        what=what,
        address=ADDRESS,
        outcome=outcome,  # type: ignore[arg-type]
        detail=detail,
    )


def test_a_full_source_reading_everything_may_say_it_is_about_the_address(
    log: Log,
) -> None:
    _read(log, "asset_transfers page 1", "empty")
    said = local._nothing_found(ADDRESS, CHAIN)
    assert ABOUT_THE_ADDRESS in said


def test_a_token_only_source_may_not(log: Log) -> None:
    """A log scan cannot see a native send. Silence there is not absence."""
    _read(log, "token transfers (logs) page 1", "empty")
    said = local._nothing_found(ADDRESS, CHAIN)
    assert ABOUT_THE_ADDRESS not in said
    assert "Native sends and internal calls emit no log" in said


def test_a_ceiling_is_named_and_disqualifies_the_claim(log: Log) -> None:
    _read(log, "asset_transfers page 1", "empty")
    _read(log, "asset_transfers page 2", "capped", "Result window is too large")
    said = local._nothing_found(ADDRESS, CHAIN)
    assert ABOUT_THE_ADDRESS not in said
    assert "ceiling" in said
    assert "Result window is too large" in said


def test_a_failure_is_named_and_disqualifies_the_claim(log: Log) -> None:
    _read(log, "asset_transfers page 1", "failed", "429 rate limited")
    said = local._nothing_found(ADDRESS, CHAIN)
    assert ABOUT_THE_ADDRESS not in said
    assert "429 rate limited" in said


def test_the_providers_that_were_asked_are_named(log: Log) -> None:
    """So the reader can tell which source the silence came from."""
    log.record(
        provider="etherscan",
        chain=str(CHAIN),
        what="asset_transfers page 1",
        address=ADDRESS,
        outcome="empty",
    )
    said = local._nothing_found(ADDRESS, CHAIN)
    assert "etherscan" in said


def test_another_addresss_reads_do_not_answer_for_this_one(log: Log) -> None:
    """The log is shared. A failure against a different address must not be
    reported as the reason this one came back empty."""
    log.record(
        provider="stub",
        chain=str(CHAIN),
        what="asset_transfers page 1",
        address="0x" + "ff" * 20,
        outcome="failed",
        detail="unrelated",
    )
    _read(log, "asset_transfers page 1", "empty")
    said = local._nothing_found(ADDRESS, CHAIN)
    assert "unrelated" not in said
    assert ABOUT_THE_ADDRESS in said


def test_no_recorded_read_is_reported_as_a_bug_not_as_an_empty_address(
    log: Log,
) -> None:
    """If nothing was recorded the tool does not know what happened, and
    guessing "the address is empty" is the failure this whole function is
    about."""
    said = local._nothing_found(ADDRESS, CHAIN)
    assert ABOUT_THE_ADDRESS not in said
    assert "bug" in said
