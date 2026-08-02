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
