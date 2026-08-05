"""What `chainscope screen` must never do: report a gap as a clean result.

Every test here is a bug that existed. The screener was written against the
`risk` package's own rules --- only an exhausted trace is a conclusion, an
absent source is not a clean answer --- and then broke each of them the first
time it was pointed at a real store, in ways that all looked like a clean bill
of health.

The one thing worth stating up front: a false `hold` costs a review, and a
false `allow` releases somebody's money. Every asymmetry below is arranged in
that direction, and the tests pin the direction rather than the wording.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from chainscope.attribution.base import Source
from chainscope.attribution.resolver import Resolver
from chainscope.chains import address_key
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ChainId
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.risk import screen, starter_policy
from chainscope.risk.decision import Action
from chainscope.risk.exposure import StopReason
from chainscope.store.sqlite import SqliteStore

CHAIN = ChainId.parse("eip155:1")
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

VICTIM = "0x1111111111111111111111111111111111111111"
FUNDER = "0x2222222222222222222222222222222222222222"
ORIGIN = "0x3333333333333333333333333333333333333333"

#: A poisoning address built to be mistaken for FUNDER: same first four hex
#: digits after 0x, same last four. This is how the real ones are chosen.
LOOKALIKE = "0x2222deadbeefdeadbeefdeadbeefdead22222222"


def _addr(raw: str) -> Address:
    """Built with the chain's own key rule, never `.lower()`.

    `Address.key` is what equality and hashing use, and folding case is right
    for EVM and destroys a base58 address --- so even a fixture goes through
    the adapter."""
    return Address(chain=CHAIN, raw=raw, key=address_key(CHAIN, raw))


def _transfer(sender: str, recipient: str, raw: int, block: int, index: int = 0) -> Transfer:
    return Transfer(
        chain=CHAIN,
        tx=TxRef(hash=f"0x{block:064x}", chain=CHAIN),
        sender=_addr(sender),
        recipient=_addr(recipient),
        amount=Amount(raw, 6, "USDC"),
        kind=TransferKind.TOKEN,
        block=block,
        index=index,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=block),
        asset=_addr(USDC),
    )


class _Fixed(Source):
    """A source that knows exactly the addresses it was given."""

    name = "fixed"
    offline = True

    def __init__(self, claims: dict[str, Attribution]) -> None:
        self._claims = {k.lower(): v for k, v in claims.items()}

    def ready(self) -> bool:
        return True

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        found = self._claims.get(address.lower())
        return [found] if found else []


def _resolver(claims: dict[str, Attribution] | None = None) -> Resolver:
    return Resolver().add(_Fixed(claims or {}))


def _claim(address: str, category: Category, label: str) -> Attribution:
    return Attribution(
        address=address,
        chain=CHAIN,
        label=label,
        category=category,
        confidence=Confidence.HIGH,
        method=Method.LABEL,
        source="a-list@2026-01-01",
        rationale="published",
    )


@pytest.fixture
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "case.db")
    yield s
    s.close()


class TestAnAbsenceIsNeverAResult:
    def test_a_walk_that_stopped_at_a_service_is_not_complete(self, store) -> None:
        """The bug this file was opened for.

        `Exposure` carries `stopped_at`, so a service boundary shows up in
        `Screen.complete` --- but only where somebody had labelled the address.
        Where nobody had, the exposure was never built, the stop went with it,
        and a walk that halted at a router reported itself complete. Found by
        running the real LpdFi case, which came back complete with the trail
        sitting against an unlabelled service.
        """
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000, 10)])
        store.mark_expanded(VICTIM, CHAIN)
        # FUNDER pays forty distinct parties: a service by shape, and nobody
        # has said a word about it.
        store.put_transfers(
            [_transfer(FUNDER, f"0x{i:040x}", 5_000, 20 + i) for i in range(45)]
        )
        store.mark_expanded(FUNDER, CHAIN)

        result = screen(store, VICTIM, CHAIN, resolver=_resolver())

        assert not result.exposures, "nobody labelled anything here"
        assert not result.complete, (
            "the walk stopped at a service and found nothing to attribute. "
            "Reporting that as complete is the same sentence as 'this deposit "
            "is clean'"
        )
        assert any("service" in gap for gap in result.unreachable_sources)

    def test_a_screen_with_no_sources_is_not_a_clean_screen(self, store) -> None:
        """A `Resolver` with no sources answers 'unknown' to everything, and
        that answer is indistinguishable from a real one downstream."""
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000, 10)])
        store.mark_expanded(VICTIM, CHAIN)
        store.mark_expanded(FUNDER, CHAIN)

        result = screen(store, VICTIM, CHAIN, resolver=Resolver())

        assert not result.complete
        assert not result.clean
        assert any("no attribution source" in gap for gap in result.unreachable_sources)

    def test_an_empty_store_says_so_about_the_store(self, store) -> None:
        """ "Nothing arrived" and "nobody looked" are opposite claims."""
        result = screen(store, VICTIM, CHAIN, resolver=_resolver())
        assert not result.complete
        assert any("never been fetched" in gap for gap in result.unreachable_sources)

    def test_allow_is_unreachable_while_the_screen_is_incomplete(self, store) -> None:
        """The floor, exercised end to end rather than on a fixture.

        A direct CEX funder is the one shape the starter policy allows. It
        still must not allow while anything is missing.
        """
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000, 10)])
        store.mark_expanded(VICTIM, CHAIN)
        # FUNDER deliberately *not* marked: its own funding was never read.

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver({FUNDER: _claim(FUNDER, Category.CEX, "An Exchange")}),
        )
        decision = starter_policy().decide(result, at=datetime.now(timezone.utc))

        assert decision.action is not Action.ALLOW
        assert decision.action is Action.HOLD


class TestTheWalkItself:
    def test_the_shortest_path_to_an_address_is_the_one_recorded(self, store) -> None:
        """Breadth-first, so hop count is a distance rather than a discovery
        order. A policy is written against that number."""
        store.put_transfers(
            [
                _transfer(FUNDER, VICTIM, 1_000_000, 10),
                _transfer(ORIGIN, VICTIM, 1_000_000, 11),
                _transfer(ORIGIN, FUNDER, 1_000_000, 5),
            ]
        )
        for who in (VICTIM, FUNDER, ORIGIN):
            store.mark_expanded(who, CHAIN)

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver({ORIGIN: _claim(ORIGIN, Category.MIXER, "A Mixer")}),
        )

        found = [e for e in result.exposures if e.source.key == ORIGIN.lower()]
        assert len(found) == 1, "one address is one exposure, at its nearest distance"
        assert found[0].hops == 0, (
            "ORIGIN pays the deposit directly as well as through FUNDER. "
            "Recording it as two hops away would overstate the distance"
        )

    def test_shares_are_proportions_of_the_deposit(self, store) -> None:
        store.put_transfers(
            [
                _transfer(FUNDER, VICTIM, 750_000, 10),
                _transfer(ORIGIN, VICTIM, 250_000, 11),
            ]
        )
        for who in (VICTIM, FUNDER, ORIGIN):
            store.mark_expanded(who, CHAIN)

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver(
                {
                    FUNDER: _claim(FUNDER, Category.MIXER, "A Mixer"),
                    ORIGIN: _claim(ORIGIN, Category.CEX, "An Exchange"),
                }
            ),
        )

        by_key = {e.source.key: e for e in result.exposures}
        assert by_key[FUNDER.lower()].share == Decimal("0.75")
        assert by_key[ORIGIN.lower()].share == Decimal("0.25")
        assert result.amount.raw == 1_000_000

    def test_poisoning_dust_does_not_become_a_funding_path(self, store) -> None:
        """Seventy-nine lookalike senders are not seventy-nine funders.

        Before the fold, every dust edge was walked and every one produced a
        line saying its origin was unknown --- so the gap list, which is the
        part a reader is meant to act on, was somebody else's spam campaign.
        """
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000_000, 10)])
        store.put_transfers(
            [_transfer(LOOKALIKE, VICTIM, 68, 11 + i, index=i) for i in range(5)]
        )
        store.mark_expanded(VICTIM, CHAIN)
        store.mark_expanded(FUNDER, CHAIN)

        result = screen(store, VICTIM, CHAIN, resolver=_resolver())

        assert not any(LOOKALIKE in gap for gap in result.unreachable_sources), (
            "dust must not appear as a funding path with an unknown origin"
        )
        assert any("folded as dust" in note for note in result.notes), (
            "and it must not disappear silently either --- the count is stated"
        )

    def test_a_labelled_exchange_ends_the_walk_as_a_service(self, store) -> None:
        """Not because it is bad. Because its funds are pooled, so what paid
        *it* says nothing about this deposit."""
        store.put_transfers(
            [
                _transfer(FUNDER, VICTIM, 1_000_000, 10),
                _transfer(ORIGIN, FUNDER, 1_000_000, 5),
            ]
        )
        for who in (VICTIM, FUNDER, ORIGIN):
            store.mark_expanded(who, CHAIN)

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver({FUNDER: _claim(FUNDER, Category.CEX, "An Exchange")}),
        )

        assert [e.source.key for e in result.exposures] == [FUNDER.lower()]
        assert result.exposures[0].stopped_at is StopReason.SERVICE
        assert not result.exposures[0].is_conclusive
        assert not any(ORIGIN in gap for gap in result.unreachable_sources), (
            "the walk stopped before ORIGIN, so it is not a gap in this screen "
            "--- it is on the other side of a boundary"
        )


class TestWhatTheStoreCanAndCannotSay:
    def test_never_followed_is_not_never_fetched(self, store) -> None:
        """The store knows one and not the other.

        `is_expanded` records that an address was *followed*. Stores written
        before that mark existed hold transfers for every address and no marks
        at all, so 'never fetched' was said about an address whose forty-five
        transfers were in the same file.
        """
        store.put_transfers(
            [
                _transfer(FUNDER, VICTIM, 1_000_000, 10),
                _transfer(ORIGIN, FUNDER, 400_000, 5),
            ]
        )
        store.mark_expanded(VICTIM, CHAIN)
        # FUNDER has inbound edges on disk and no mark --- exactly the shape.

        result = screen(store, VICTIM, CHAIN, resolver=_resolver())

        gaps = " ".join(result.unreachable_sources)
        assert "never followed" in gaps
        assert "never been fetched" not in gaps


class TestTheDecisionItProduces:
    def test_a_sanctioned_direct_funder_is_rejected(self, store) -> None:
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000, 10)])
        store.mark_expanded(VICTIM, CHAIN)
        store.mark_expanded(FUNDER, CHAIN)

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver(
                {FUNDER: _claim(FUNDER, Category.SANCTIONED, "A Listed Entity")}
            ),
        )
        decision = starter_policy().decide(result, at=datetime.now(timezone.utc))

        assert decision.action is Action.REJECT
        assert decision.rule_id == "sanctioned-direct"

    def test_the_counterfactual_names_the_load_bearing_source(self, store) -> None:
        """The line a compliance officer needs and a score cannot produce."""
        store.put_transfers([_transfer(FUNDER, VICTIM, 1_000_000, 10)])
        store.mark_expanded(VICTIM, CHAIN)
        store.mark_expanded(FUNDER, CHAIN)

        result = screen(
            store,
            VICTIM,
            CHAIN,
            resolver=_resolver(
                {FUNDER: _claim(FUNDER, Category.SANCTIONED, "A Listed Entity")}
            ),
        )
        decision = starter_policy().decide(result, at=datetime.now(timezone.utc))

        assert any(c.without == "a-list@2026-01-01" for c in decision.counterfactuals), (
            "without that one tag the answer differs, and saying so is the "
            "whole difference between a finding and a number"
        )


class TestChoosingWhichAssetToScreen:
    """Raw integers of different assets are different things."""

    #: An eighteen-decimal token. One whole unit of it is 1e18 raw; 689,529
    #: units of a six-decimal stablecoin is 6.9e11. Ranked on raw, the token
    #: with the most decimals wins every time regardless of quantity.
    WIDE = "0xdddddddddddddddddddddddddddddddddddddddd"

    def _mixed(self, store) -> None:
        store.put_transfers([_transfer(FUNDER, VICTIM, 689_529_000_000, 10)])
        store.put_transfers(
            [
                Transfer(
                    chain=CHAIN,
                    tx=TxRef(hash=f"0x{20:064x}", chain=CHAIN),
                    sender=_addr(ORIGIN),
                    recipient=_addr(VICTIM),
                    amount=Amount(2 * 10**18, 18, "WIDE"),
                    kind=TransferKind.TOKEN,
                    block=20,
                    timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    asset=_addr(self.WIDE),
                )
            ]
        )
        store.mark_expanded(VICTIM, CHAIN)

    def test_the_asset_is_ranked_by_quantity_not_by_raw_units(self, store) -> None:
        self._mixed(store)
        result = screen(store, VICTIM, CHAIN, resolver=_resolver())
        assert result.amount.symbol == "USDC", (
            "689,529 USDC against 2 WIDE. Ranked on raw integers the two-unit "
            "token wins because it carries twelve more decimals, which is a "
            "property of its encoding rather than of the deposit"
        )

    def test_it_says_which_asset_it_passed_over(self, store) -> None:
        self._mixed(store)
        result = screen(store, VICTIM, CHAIN, resolver=_resolver())
        assert any("WIDE" in note for note in result.notes)

    def test_the_caller_can_name_the_asset(self, store) -> None:
        self._mixed(store)
        result = screen(store, VICTIM, CHAIN, resolver=_resolver(), asset=self.WIDE)
        assert result.amount.symbol == "WIDE"
        assert result.amount.raw == 2 * 10**18

    def test_an_asset_that_never_arrived_is_not_a_clean_screen(self, store) -> None:
        self._mixed(store)
        result = screen(store, VICTIM, CHAIN, resolver=_resolver(), asset="0x" + "ab" * 20)
        assert not result.clean
        assert not result.complete
        assert any("nothing arrived" in gap for gap in result.unreachable_sources)
