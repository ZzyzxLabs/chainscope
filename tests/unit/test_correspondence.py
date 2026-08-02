"""The correspondence ledger: the clock, and the three refusals that shape it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chainscope.case.correspondence import (
    Ledger,
    Request,
    RequestEvent,
    RequestKind,
    Status,
)

SENT = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def request(**kw: object) -> Request:
    fields: dict[str, object] = {
        "counterparty": "Binance",
        "kind": RequestKind.FREEZE,
        "sent_at": SENT,
        "analyst": "alice@lab",
        "identified_by": "env",
    }
    fields.update(kw)
    return Request(**fields)  # type: ignore[arg-type]


def event(status: Status, **kw: object) -> RequestEvent:
    fields: dict[str, object] = {
        "at": NOW,
        "status": status,
        "analyst": "alice@lab",
        "identified_by": "env",
        "body": "they replied",
    }
    fields.update(kw)
    return RequestEvent(**fields)  # type: ignore[arg-type]


class TestRequest:
    def test_needs_a_counterparty(self) -> None:
        with pytest.raises(ValueError, match="who it went to"):
            request(counterparty="  ")

    def test_a_deadline_before_the_send_date_is_refused(self) -> None:
        # Both dates are typed by hand and one of them is wrong; guessing which
        # would put a request on the overdue list the day it was created.
        with pytest.raises(ValueError, match="one of the two dates is wrong"):
            request(due_at=SENT - timedelta(days=1))


class TestSilenceIsNotRefusal:
    def test_no_reply_leaves_it_open(self) -> None:
        assert request().status is None
        assert request().is_open

    def test_a_refusal_closes_it(self) -> None:
        # And the two are distinguishable, which is the point: only a refusal
        # is a decision somebody made and can be escalated against.
        refused = request(events=(event(Status.REFUSED),))
        assert refused.status is Status.REFUSED
        assert not refused.is_open

    def test_partial_stays_open(self) -> None:
        partial = request(events=(event(Status.PARTIAL),))
        assert partial.is_open

    def test_acknowledged_is_not_an_answer(self) -> None:
        assert request(events=(event(Status.ACKNOWLEDGED, body=""),)).is_open


class TestContentIsRequired:
    @pytest.mark.parametrize("status", [Status.ANSWERED, Status.REFUSED, Status.PARTIAL])
    def test_closing_without_saying_what_happened(self, status: Status) -> None:
        with pytest.raises(ValueError, match="say what happened"):
            event(status, body="   ")

    def test_acknowledged_needs_nothing(self) -> None:
        # "They have it" carries its whole meaning in the status.
        assert event(Status.ACKNOWLEDGED, body="").status is Status.ACKNOWLEDGED


class TestTheClock:
    def test_overdue_is_derived_from_the_date(self) -> None:
        late = request(due_at=SENT + timedelta(days=7))
        assert late.overdue_at(NOW)
        assert not late.overdue_at(SENT + timedelta(days=1))

    def test_a_closed_request_is_never_overdue(self) -> None:
        closed = request(
            due_at=SENT + timedelta(days=7),
            events=(event(Status.ANSWERED, at=SENT + timedelta(days=2)),),
        )
        assert not closed.overdue_at(NOW)

    def test_no_deadline_is_never_overdue(self) -> None:
        assert not request().overdue_at(NOW)

    def test_age_stops_at_the_reply(self) -> None:
        # Otherwise a finished request outranks a genuinely stale one on a list
        # sorted by age, which is the list's whole purpose.
        answered = request(events=(event(Status.ANSWERED, at=SENT + timedelta(days=3)),))
        assert answered.age_days(NOW) == 3
        assert request().age_days(NOW) == 31

    def test_a_non_closing_event_does_not_stop_the_clock(self) -> None:
        chased = request(events=(event(Status.ACKNOWLEDGED, at=SENT + timedelta(days=1)),))
        assert chased.age_days(NOW) == 31


class TestLedger:
    def test_roundtrip(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "case.db")
        rid = ledger.send(
            request(subject="0xABC", reference="T-1", due_at=SENT + timedelta(days=7))
        )
        stored = ledger.get(rid)
        assert stored is not None
        assert stored.counterparty == "Binance"
        assert stored.subject == "0xabc"
        assert stored.reference == "T-1"
        assert stored.due_at == SENT + timedelta(days=7)
        ledger.close()

    def test_events_come_back_in_order(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "case.db")
        rid = ledger.send(request())
        ledger.record(rid, event(Status.ACKNOWLEDGED, at=SENT + timedelta(days=1), body=""))
        ledger.record(rid, event(Status.ANSWERED, at=SENT + timedelta(days=5)))
        stored = ledger.get(rid)
        assert stored is not None
        assert [e.status for e in stored.events] == [Status.ACKNOWLEDGED, Status.ANSWERED]
        ledger.close()

    def test_a_closed_request_cannot_be_reopened(self, tmp_path: Path) -> None:
        # Appending past a close would make `status` a lie about what happened.
        ledger = Ledger(tmp_path / "case.db")
        rid = ledger.send(request())
        ledger.record(rid, event(Status.REFUSED))
        with pytest.raises(ValueError, match="Send a new request"):
            ledger.record(rid, event(Status.ANSWERED))
        ledger.close()

    def test_an_unknown_request_is_named(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "case.db")
        with pytest.raises(ValueError, match="no request 42"):
            ledger.record(42, event(Status.ANSWERED))
        ledger.close()

    def test_open_only_filters(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "case.db")
        open_id = ledger.send(request(counterparty="OKX"))
        closed = ledger.send(request(counterparty="Kraken"))
        ledger.record(closed, event(Status.ANSWERED))
        assert [r.id for r in ledger.requests(open_only=True)] == [open_id]
        assert len(ledger.requests()) == 2
        ledger.close()

    def test_subject_lookup_is_case_insensitive(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "case.db")
        ledger.send(request(subject="0xAbCd"))
        assert len(ledger.requests(subject="0xABCD")) == 1
        ledger.close()

    def test_shares_a_file_with_the_case_log(self, tmp_path: Path) -> None:
        # Both are things a person wrote; neither is rebuildable from the cache.
        from chainscope.case.log import CaseLog, Note, NoteKind

        path = tmp_path / "case.db"
        log = CaseLog(path)
        log.add(
            Note(
                at=NOW,
                analyst="alice@lab",
                identified_by="env",
                kind=NoteKind.OBSERVATION,
                body="asked Binance to freeze",
            )
        )
        log.close()

        ledger = Ledger(path)
        ledger.send(request())
        assert len(ledger.requests()) == 1
        ledger.close()

        again = CaseLog(path)
        assert len(again.notes()) == 1
        again.close()


class TestDeadlineSemantics:
    def test_a_due_date_means_the_end_of_that_day(self) -> None:
        """Parsed at midnight, a request is overdue all through the day it is due.

        `request list` would then put it under the `!` marker twenty-four hours
        early --- and that number is the one thing this command exists to
        report accurately.
        """
        from chainscope.cli.commands.request import _date

        due = _date("2026-07-09", "due")
        assert due is not None
        assert (due.hour, due.minute) == (23, 59)

        req = request(sent_at=SENT, due_at=due)
        midday = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        assert not req.overdue_at(midday)
        assert req.overdue_at(datetime(2026, 7, 10, 0, 1, tzinfo=timezone.utc))

    def test_a_send_date_stays_at_the_start_of_the_day(self) -> None:
        # It starts the clock, and a request recorded as sent on the 2nd went
        # out at some point during the 2nd.
        from chainscope.cli.commands.request import _date

        sent = _date("2026-07-02", "sent")
        assert sent is not None
        assert (sent.hour, sent.minute) == (0, 0)

    def test_same_day_send_and_deadline_is_accepted(self) -> None:
        # With end-of-day semantics this is a real same-day deadline rather
        # than a contradiction.
        from chainscope.cli.commands.request import _date

        sent, due = _date("2026-07-02", "sent"), _date("2026-07-02", "due")
        assert request(sent_at=sent, due_at=due).is_open


class TestConcurrentReplies:
    def test_two_threads_cannot_both_close_one_request(self, tmp_path: Path) -> None:
        """Check-then-append under one lock.

        Split, both threads read the request as open and both close it --- and
        the second close is the one `status` reports, silently.
        """
        import threading

        ledger = Ledger(tmp_path / "case.db")
        rid = ledger.send(request())

        start = threading.Barrier(2)
        outcomes: list[str] = []

        def close(which: str) -> None:
            start.wait()
            try:
                ledger.record(rid, event(Status.ANSWERED, body=which))
                outcomes.append("recorded")
            except ValueError:
                outcomes.append("refused")

        threads = [threading.Thread(target=close, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == ["recorded", "refused"]
        stored = ledger.get(rid)
        assert stored is not None
        assert len(stored.events) == 1
        ledger.close()


class TestTheClockReadsAccurately:
    """`timedelta.days` floors, and this is the one command whose job is a clock."""

    def _at(self, hours: float) -> str:
        from chainscope.cli.commands.request import _left

        due = datetime(2026, 7, 9, 23, 59, tzinfo=timezone.utc)
        return _left(due, due - timedelta(hours=hours))

    def test_a_deadline_this_afternoon_is_not_a_whole_day_away(self) -> None:
        # `(now - due).days` gave "1d left" for anything under twenty-four
        # hours, including a request due in three hours.
        assert self._at(3) == "3h left"

    def test_under_an_hour_says_so(self) -> None:
        assert self._at(0.5) == "under an hour"

    def test_days_once_it_is_days(self) -> None:
        assert self._at(50) == "2d left"

    def test_overdue_reads_the_same_way(self) -> None:
        assert self._at(-5) == "5h past"
        assert self._at(-49) == "2d past"
