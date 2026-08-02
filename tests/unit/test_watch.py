"""Watches.

The property everything else rests on is replayability: the same store and the
same block range must produce the same events, forever. That is what makes
"why did this fire?" answerable months later, and an alerting system that
cannot reconstruct its own past decisions is not usable as evidence.

So the tests care most about determinism, about the range being exactly what it
says, and about events carrying enough to re-derive themselves.
"""

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore
from chainscope.watch import (
    AmountOver,
    AnyOf,
    CounterpartyIn,
    CounterpartyIsUnknown,
    Severity,
    TouchesCategory,
    Watch,
    WatchError,
    evaluate,
    evaluate_all,
)
from chainscope.watch.base import AllOf

SUBJECT = "0x" + "a" * 40
CLEAN = "0x" + "b" * 40
MIXER = "0x" + "c" * 40

ONE_ETH = 10**18
TEN_ETH = 10 * ONE_ETH


def transfer(sender, recipient, raw, *, block, symbol="ETH", decimals=18, i=0):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.NATIVE,
        block=block,
        index=i,
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "store.db")
    s.put_transfers(
        [
            transfer(SUBJECT, CLEAN, ONE_ETH, block=100),
            transfer(SUBJECT, MIXER, TEN_ETH * 50, block=200),
            transfer(CLEAN, SUBJECT, ONE_ETH * 2, block=300),
            transfer(SUBJECT, CLEAN, 5_000_000, block=400, symbol="USDC", decimals=6),
        ],
        source="test",
    )
    s.put_attributions(
        [
            Attribution(
                label="Tornado Cash",
                category=Category.MIXER,
                confidence=Confidence.HIGH,
                method=Method.LIST,
                source="OFAC SDN",
                address=MIXER,
                chain=ETHEREUM,
            ),
            Attribution(
                label="Binance",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LABEL,
                source="etherscan",
                address=CLEAN,
                chain=ETHEREUM,
            ),
        ]
    )
    yield s
    s.close()


def watch(predicate, **kw):
    return Watch(
        name=kw.pop("name", "test-watch"),
        subject=kw.pop("subject", SUBJECT),
        predicate=predicate,
        chain=ETHEREUM,
        **kw,
    )


class TestRangeIsExactlyWhatItSays:
    def test_the_range_is_inclusive_at_both_ends(self, store):
        """A half-open range makes it easy for a caller stepping through
        history to skip exactly one block per step, and nobody notices until an
        alert did not fire."""
        events = evaluate(watch(AmountOver(0)), store, 100, 100)
        assert [e.transfer.block for e in events] == [100]

    def test_blocks_outside_the_range_are_not_evaluated(self, store):
        events = evaluate(watch(AmountOver(0)), store, 150, 350)
        assert sorted(e.transfer.block for e in events) == [200, 300]

    def test_an_inverted_range_is_refused(self, store):
        with pytest.raises(WatchError, match="empty range"):
            evaluate(watch(AmountOver(0)), store, 500, 100)

    def test_an_empty_range_yields_nothing_rather_than_failing(self, store):
        assert evaluate(watch(AmountOver(0)), store, 900, 950) == []


class TestReplayability:
    def test_the_same_range_gives_the_same_events(self, store):
        """The guarantee the whole design exists for."""
        w = watch(AmountOver(ONE_ETH))
        first = [e.to_dict() for e in evaluate(w, store, 0, 1000)]
        second = [e.to_dict() for e in evaluate(w, store, 0, 1000)]
        assert first == second

    def test_an_event_carries_the_range_that_produced_it(self, store):
        """Without it there is nothing to re-run."""
        (event, *_) = evaluate(watch(AmountOver(ONE_ETH)), store, 50, 500)
        assert (event.since, event.until) == (50, 500)
        assert event.to_dict()["range"] == [50, 500]

    def test_an_event_names_the_transaction(self, store):
        (event, *_) = evaluate(watch(AmountOver(ONE_ETH)), store, 0, 1000)
        assert event.transfer.tx.hash.startswith("0x")
        assert event.to_dict()["tx"] == event.transfer.tx.hash

    def test_amounts_serialise_as_strings(self, store):
        """50 ETH here; a JSON number would arrive rounded."""
        (event, *_) = evaluate(watch(AmountOver(TEN_ETH)), store, 0, 1000)
        raw = event.to_dict()["amount"]
        assert isinstance(raw, str)
        assert int(raw) == TEN_ETH * 50

    def test_a_watch_serialises_with_its_rule(self, store):
        """A rule nobody can read six months later is a rule nobody can
        defend."""
        w = watch(TouchesCategory(Category.MIXER), severity=Severity.URGENT)
        data = w.to_dict()
        assert data["predicate"]["kind"] == "touches_category"
        assert "mixer" in data["describes"]
        assert data["severity"] == "urgent"


class TestPredicates:
    def test_amount_over_uses_raw_units(self, store):
        events = evaluate(watch(AmountOver(TEN_ETH)), store, 0, 1000)
        assert [e.transfer.block for e in events] == [200]

    def test_amount_over_can_scope_to_one_asset(self, store):
        """A threshold meaningful for ETH is meaningless for USDC."""
        events = evaluate(watch(AmountOver(1_000_000, symbol="USDC")), store, 0, 1000)
        assert [e.transfer.block for e in events] == [400]

    def test_touches_category_reports_which_side_and_why(self, store):
        (event,) = evaluate(watch(TouchesCategory(Category.MIXER)), store, 0, 1000)
        assert "recipient" in event.reason
        assert "Tornado Cash" in event.reason
        assert "HIGH" in event.reason
        assert "OFAC SDN" in event.reason

    def test_confidence_below_the_floor_does_not_fire(self, store):
        w = watch(TouchesCategory(Category.MIXER, min_confidence=Confidence.CERTAIN))
        assert evaluate(w, store, 0, 1000) == []

    def test_counterparty_in_a_named_set(self, store):
        w = watch(CounterpartyIn(frozenset({MIXER.lower()}), label="known mixers"))
        (event,) = evaluate(w, store, 0, 1000)
        assert "known mixers" in event.reason

    def test_unlabelled_counterparties_are_a_first_class_rule(self, tmp_path):
        """Funds moving to an address nobody has labelled is the ordinary shape
        of a new laundering route; a setup built only from known-bad lists
        cannot see it."""
        s = SqliteStore(tmp_path / "s.db")
        unknown = "0x" + "9" * 40
        s.put_transfers([transfer(SUBJECT, unknown, ONE_ETH, block=10)], source="t")
        try:
            (event,) = evaluate(watch(CounterpartyIsUnknown()), s, 0, 100)
            assert "no attribution" in event.reason
        finally:
            s.close()

    def test_a_labelled_counterparty_does_not_fire_the_unknown_rule(self, store):
        w = watch(CounterpartyIsUnknown(), subject=SUBJECT)
        assert evaluate(w, store, 0, 1000) == []

    def test_the_counterparty_is_the_other_side_not_a_fixed_side(self, store):
        """On an inbound transfer the subject *is* the recipient. A rule that
        always inspected the recipient would examine the subject, which is
        rarely labelled in its own store, and fire on every inbound transfer."""
        w = watch(CounterpartyIsUnknown(), subject=SUBJECT, direction="in")
        assert evaluate(w, store, 0, 1000) == []

    def test_any_of_joins_the_reasons(self, store):
        w = watch(AnyOf((AmountOver(TEN_ETH), TouchesCategory(Category.MIXER))))
        (event,) = evaluate(w, store, 0, 1000)
        assert ";" in event.reason

    def test_all_of_requires_every_child(self, store):
        both = watch(AllOf((AmountOver(TEN_ETH), TouchesCategory(Category.MIXER))))
        assert len(evaluate(both, store, 0, 1000)) == 1

        neither = watch(AllOf((AmountOver(TEN_ETH), TouchesCategory(Category.CEX))))
        assert evaluate(neither, store, 0, 1000) == []

    def test_a_reason_is_always_present_when_a_watch_fires(self, store):
        """An alert saying only "watch fired" sends the reader back to the data
        to work out why."""
        for e in evaluate(watch(AmountOver(0)), store, 0, 1000):
            assert e.reason and len(e.reason) > 10


class TestDirection:
    def test_out_only(self, store):
        w = watch(AmountOver(0), direction="out")
        assert all(e.transfer.sender.key == SUBJECT for e in evaluate(w, store, 0, 1000))

    def test_in_only(self, store):
        w = watch(AmountOver(0), direction="in")
        blocks = [e.transfer.block for e in evaluate(w, store, 0, 1000)]
        assert blocks == [300]

    def test_both_sees_everything(self, store):
        w = watch(AmountOver(0), direction="both")
        assert len(evaluate(w, store, 0, 1000)) == 4

    def test_a_bad_direction_is_refused_at_construction(self):
        with pytest.raises(WatchError, match="direction"):
            watch(AmountOver(0), direction="sideways")


class TestConstruction:
    def test_a_watch_needs_a_name(self):
        """It appears in every event it raises."""
        with pytest.raises(WatchError, match="needs a name"):
            watch(AmountOver(0), name="  ")

    def test_a_watch_needs_a_subject(self):
        with pytest.raises(WatchError, match="needs a subject"):
            watch(AmountOver(0), subject="")


class TestSeverity:
    def test_severity_is_not_confidence(self, store):
        """Confidence is how sure we are the claim is true; severity is how
        much it matters if it is."""
        w = watch(TouchesCategory(Category.MIXER), severity=Severity.URGENT)
        (event,) = evaluate(w, store, 0, 1000)
        assert event.severity is Severity.URGENT

    def test_evaluate_all_puts_the_urgent_first(self, store):
        """A list that buries the urgent match under forty informational ones
        has not alerted anybody."""
        watches = [
            watch(AmountOver(0), name="everything", severity=Severity.INFO),
            watch(TouchesCategory(Category.MIXER), name="mixer", severity=Severity.URGENT),
        ]
        events = evaluate_all(watches, store, 0, 1000)
        assert events[0].severity is Severity.URGENT

    def test_evaluate_all_shares_one_lookup_cache(self, store):
        """A watch over a thousand transfers from one address would otherwise
        ask about the same counterparty a thousand times."""
        calls = {"n": 0}
        original = store.attributions

        def counted(address):
            calls["n"] += 1
            return original(address)

        store.attributions = counted  # type: ignore[method-assign]
        try:
            evaluate_all(
                [
                    watch(TouchesCategory(Category.MIXER), name="a"),
                    watch(TouchesCategory(Category.CEX), name="b"),
                ],
                store,
                0,
                1000,
            )
        finally:
            store.attributions = original  # type: ignore[method-assign]
        # Four transfers, two addresses each, two watches: without memoisation
        # this would be sixteen.
        assert calls["n"] <= 4
