"""Finding the real path through an address that is mostly somebody else's spam.

The LpdFi attacker's address carried 85 transfers and six that mattered. These
hold the three rules that get from one to the other, and the one that must
*not* be applied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from chainscope.analysis.trail import Direction, SetAside, trail

REAL = 689_429_793_148_448_987_344_168
TEST = 100 * 10**18
DUST = 68_942_979_314_844
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
FORGED = "0x34a7cc385dccb0f034c49b9a2fc8d0c747705e2f"

ME = "0x" + "5d" * 20
PEER = "0x" + "a1" * 20
POISONER = "0x" + "a2" * 20


def row(
    sender: str,
    recipient: str,
    raw: int,
    block: int,
    asset: str = USDC,
    symbol: str = "USDC",
) -> Any:
    return SimpleNamespace(
        sender=SimpleNamespace(raw=sender, key=sender.lower()),
        recipient=SimpleNamespace(raw=recipient, key=recipient.lower()),
        amount=SimpleNamespace(raw=raw, symbol=symbol, decimals=18),
        asset=SimpleNamespace(raw=asset, key=asset.lower()),
        block=block,
        index=0,
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
        tx=SimpleNamespace(hash=f"0x{block:064x}"),
    )


def test_both_directions_are_reported() -> None:
    """Who funded the address is frequently the more useful half --- the LpdFi
    attacker was staked before the exploit, and every manual trace that started
    at the exploit block missed it."""
    found = trail([row(PEER, ME, REAL, 1), row(ME, PEER, REAL, 2)], ME)
    assert len(found.funding) == 1
    assert len(found.onward) == 1
    assert found.funding[0].direction is Direction.IN


def test_steps_are_oldest_first() -> None:
    found = trail([row(ME, PEER, REAL, 9), row(PEER, ME, REAL, 3)], ME)
    assert [s.block for s in found.steps] == [3, 9]


# ---------------------------------------------------------------- set aside


def test_a_zero_value_transfer_is_set_aside() -> None:
    """It moves nothing, so there is nothing to trace. A real log from the real
    contract --- anyone may call transfer(victim, 0) --- which is why neither a
    symbol check nor a contract check sees it."""
    found = trail([row(POISONER, ME, 0, 1), row(PEER, ME, REAL, 2)], ME)
    assert found.set_aside[SetAside.ZERO] == 1
    assert len(found.steps) == 1


def test_a_forged_asset_is_set_aside_and_named() -> None:
    """Its logs say whatever its author chose, including who sent them."""
    rows = [
        row(PEER, ME, REAL, 1),
        row(POISONER, ME, REAL, 2, asset=FORGED, symbol="ÚЅDC"),
    ]
    found = trail(rows, ME)
    assert found.set_aside.get(SetAside.FORGED_ASSET) == 1
    assert FORGED in found.forged_assets


def test_an_unlisted_token_is_not_set_aside() -> None:
    """Most tokens are in no registry and are entirely real. Removing them
    would invert the error the impersonation check is careful about."""
    odd = "0x" + "99" * 20
    found = trail([row(PEER, ME, REAL, 1, asset=odd, symbol="PIZZA")], ME)
    assert not found.set_aside
    assert len(found.steps) == 1


# -------------------------------------------------- folded, never deleted


def test_a_test_payment_is_not_folded_away() -> None:
    """The rule the first threshold got wrong. 100 USDC against a 689,429
    payout is 1.45e-4 of the flow and is the most incriminating movement in the
    case; poisoning dust is 1e-10. Six orders of magnitude apart."""
    rows = [row(ME, PEER, TEST, 1), row(ME, PEER, REAL, 2)]
    found = trail(rows, ME)
    assert [s.amount_raw for s in found.significant] == [TEST, REAL]


def test_dust_is_folded() -> None:
    rows = [row(POISONER, ME, DUST, 1), row(PEER, ME, REAL, 2)]
    found = trail(rows, ME)
    assert [s.amount_raw for s in found.significant] == [REAL]


def test_folded_steps_are_still_present() -> None:
    """Marked, not removed: an amount engineered to resemble the real one is
    evidence of who was targeted, not noise."""
    rows = [row(POISONER, ME, DUST, 1), row(PEER, ME, REAL, 2)]
    found = trail(rows, ME)
    assert len(found.steps) == 2
    assert sum(1 for s in found.steps if s.minor) == 1


def test_the_threshold_is_per_asset() -> None:
    """A threshold shared across assets compares raw units of things with
    different decimals."""
    other = "0x" + "77" * 20
    rows = [
        row(PEER, ME, REAL, 1),
        row(PEER, ME, 5 * 10**18, 2, asset=other, symbol="PIZZA"),
    ]
    found = trail(rows, ME)
    # The PIZZA transfer is tiny next to the USDC one and must not be folded
    # by it: it is the largest PIZZA movement there is.
    assert len(found.significant) == 2


# -------------------------------------------------------------- the ratio


def test_the_summary_states_what_was_removed_and_what_was_folded() -> None:
    """A path that silently became legible is one somebody trusts for the wrong
    reason."""
    rows = [
        row(PEER, ME, REAL, 1),
        row(POISONER, ME, 0, 2),
        row(POISONER, ME, DUST, 3),
        row(POISONER, ME, REAL, 4, asset=FORGED, symbol="ÚЅDC"),
    ]
    said = trail(rows, ME).summary()
    assert "zero-value" in said
    assert "forged-asset" in said
    assert "minor" in said
    assert "not deleted" in said


def test_considered_counts_everything_that_touched_the_address() -> None:
    rows = [row(PEER, ME, REAL, 1), row(POISONER, ME, 0, 2)]
    assert trail(rows, ME).considered == 2


def test_transfers_that_do_not_touch_the_address_are_ignored_silently() -> None:
    """Not 'set aside' --- they were never candidates, and counting them would
    make the ratio meaningless."""
    other = "0x" + "cc" * 20
    found = trail([row(PEER, other, REAL, 1)], ME)
    assert found.considered == 0
    assert not found.set_aside
