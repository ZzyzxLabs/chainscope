"""What was asked of whom, when, and whether it came back.

A tool that traces funds into an exchange and cannot record what was asked of
the exchange holding them stops one step short of the point. Tracing ends at a
custodial deposit; the case does not. What happens next is a KYC request, a
freeze request, a preservation letter --- and a clock, which runs while the
money keeps moving.

This is case management rather than chain analysis, and it is deliberately a
ledger rather than an analyzer.

**Three refusals shape it.**

*Overdue is derived, never stored.* A status field containing "expired" is only
true if somebody remembered to run something. Computed from the deadline, it is
true the moment it becomes true.

*No answer is not a refusal.* A request nobody has replied to and a request
somebody declined are different facts about a case and lead to different next
moves. Collapsing them into "unsuccessful" loses the one that is still worth
chasing.

*An answer needs its content.* Recording that a request came back, without what
came back in it, is indistinguishable from not having read the reply --- so
closing a request requires saying what the answer was.

**Append-only, like the case log it sits beside.** Status changes are events,
not an overwritten column, because *when* a freeze was confirmed is frequently
the fact in dispute.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

__all__ = ["Ledger", "Request", "RequestEvent", "RequestKind", "Status"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY,
    counterparty  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    chain         TEXT,
    -- Separate from the row's creation time on purpose: a request is usually
    -- recorded after it was sent, and a clock started at the moment somebody
    -- got round to typing it in is the wrong clock.
    sent_at       INTEGER NOT NULL,
    due_at        INTEGER,
    reference     TEXT NOT NULL DEFAULT '',
    analyst       TEXT NOT NULL,
    identified_by TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_req_subject ON requests(subject);
CREATE INDEX IF NOT EXISTS ix_req_sent    ON requests(sent_at);

-- Status is a sequence of events rather than a column, because *when* a freeze
-- was confirmed is regularly the fact in dispute, and an overwritten column
-- cannot answer it.
CREATE TABLE IF NOT EXISTS request_events (
    id            INTEGER PRIMARY KEY,
    request       INTEGER NOT NULL REFERENCES requests(id),
    at            INTEGER NOT NULL,
    status        TEXT NOT NULL,
    analyst       TEXT NOT NULL,
    identified_by TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_ev_request ON request_events(request, at);
"""


class RequestKind(Enum):
    """What was asked for."""

    KYC = "kyc"
    """Subscriber information --- who holds the account."""

    FREEZE = "freeze"
    """A hold on funds. The one where the clock matters most."""

    RECORDS = "records"
    """Transaction history, login records, linked accounts."""

    PRESERVATION = "preservation"
    """Keep the data; the legal process to obtain it is still coming. Cheap to
    send and routinely forgotten until the retention window has passed."""


class Status(Enum):
    """Where a request has got to.

    `OVERDUE` is deliberately absent: it is derived from the deadline, because a
    stored one is only correct if somebody remembered to update it.
    """

    ACKNOWLEDGED = "acknowledged"
    """They have it. Not an answer."""

    PARTIAL = "partial"
    """Some of what was asked for. Still open --- the remainder is the point."""

    ANSWERED = "answered"
    """Closed, with content. Requires saying what the answer was."""

    REFUSED = "refused"
    """Closed, declined. Distinct from silence: a refusal is a decision somebody
    made and can be escalated against; silence cannot."""

    WITHDRAWN = "withdrawn"
    """Closed by us. Kept rather than deleted --- that a request was made and
    then dropped is part of the record."""

    @property
    def closes(self) -> bool:
        return self in _CLOSING

    @property
    def needs_content(self) -> bool:
        """Whether this status is meaningless without saying what happened.

        An answer recorded with no content cannot be told apart from a reply
        nobody read, and a refusal with no reason cannot be escalated against.
        """
        return self in (Status.ANSWERED, Status.REFUSED, Status.PARTIAL)


_CLOSING = frozenset({Status.ANSWERED, Status.REFUSED, Status.WITHDRAWN})


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """One thing that happened to a request."""

    at: datetime
    status: Status
    analyst: str
    identified_by: str
    body: str = ""
    request: int = 0
    id: int = 0

    def __post_init__(self) -> None:
        if self.status.needs_content and not self.body.strip():
            raise ValueError(
                f"a {self.status.value} request needs to say what happened "
                f"(--note). Recorded without it, it cannot be told apart from a "
                f"reply nobody read"
            )


@dataclass(frozen=True, slots=True)
class Request:
    """One thing asked of one counterparty."""

    counterparty: str
    kind: RequestKind
    sent_at: datetime
    subject: str = ""
    chain: str | None = None
    due_at: datetime | None = None
    reference: str = ""
    analyst: str = ""
    identified_by: str = ""
    body: str = ""
    id: int = 0
    events: tuple[RequestEvent, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.counterparty.strip():
            raise ValueError("a request needs a counterparty --- who it went to")
        if self.due_at and self.due_at < self.sent_at:
            raise ValueError(
                f"the deadline ({self.due_at:%Y-%m-%d}) is before the request was "
                f"sent ({self.sent_at:%Y-%m-%d}); one of the two dates is wrong"
            )

    @property
    def status(self) -> Status | None:
        """The latest event's status, or ``None`` --- sent, nothing back."""
        return self.events[-1].status if self.events else None

    @property
    def is_open(self) -> bool:
        status = self.status
        return status is None or not status.closes

    def overdue_at(self, now: datetime) -> bool:
        """Past its deadline and still open.

        Derived rather than stored. A request is not overdue because somebody
        ran a sweep; it is overdue because the date passed.
        """
        return bool(self.due_at and self.is_open and now > self.due_at)

    def age_days(self, now: datetime) -> int:
        """Days since it was sent --- or until it closed.

        A clock that keeps running after an answer arrived would report a
        finished request as the oldest thing on the list.
        """
        end = now
        for event in self.events:
            if event.status.closes:
                end = event.at
                break
        return max(0, (end - self.sent_at).days)


class Ledger:
    """Correspondence for one case. Append-only, beside the case log."""

    def __init__(self, path: Path | str = ".chainscope/case.db") -> None:
        self.path = Path(path) if path != ":memory:" else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path) if self.path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def send(self, request: Request) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO requests (counterparty, kind, subject, chain, sent_at, "
                " due_at, reference, analyst, identified_by, body) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.counterparty,
                    request.kind.value,
                    request.subject.lower(),
                    request.chain,
                    int(request.sent_at.timestamp()),
                    int(request.due_at.timestamp()) if request.due_at else None,
                    request.reference,
                    request.analyst,
                    request.identified_by,
                    request.body,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def record(self, request_id: int, event: RequestEvent) -> int:
        """Append an event. The request must exist and must still be open."""
        existing = self.get(request_id)
        if existing is None:
            raise ValueError(f"no request {request_id} in {self.path}")
        if not existing.is_open:
            # Reopening by appending past a close would make `status` a lie
            # about what happened; a new request is the honest way to chase one
            # that was refused.
            closed = existing.status.value if existing.status else "closed"
            raise ValueError(
                f"request {request_id} was {closed} on "
                f"{existing.events[-1].at:%Y-%m-%d}. Send a new request rather "
                f"than reopening this one, so the first exchange stays legible"
            )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO request_events "
                "(request, at, status, analyst, identified_by, body) VALUES (?,?,?,?,?,?)",
                (
                    request_id,
                    int(event.at.timestamp()),
                    event.status.value,
                    event.analyst,
                    event.identified_by,
                    event.body,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def get(self, request_id: int) -> Request | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return self._hydrate(row) if row else None

    def requests(self, *, subject: str = "", open_only: bool = False) -> list[Request]:
        """Every request, oldest first."""
        sql = "SELECT * FROM requests"
        params: list[object] = []
        if subject:
            sql += " WHERE subject = ?"
            params.append(subject.lower())
        sql += " ORDER BY sent_at, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [self._hydrate(row) for row in rows]
        return [r for r in out if r.is_open] if open_only else out

    def _hydrate(self, row: sqlite3.Row) -> Request:
        with self._lock:
            events = self._conn.execute(
                "SELECT * FROM request_events WHERE request = ? ORDER BY at, id",
                (row["id"],),
            ).fetchall()
        return Request(
            id=int(row["id"]),
            counterparty=row["counterparty"],
            kind=RequestKind(row["kind"]),
            subject=row["subject"] or "",
            chain=row["chain"],
            sent_at=_dt(row["sent_at"]),
            due_at=_dt(row["due_at"]) if row["due_at"] is not None else None,
            reference=row["reference"],
            analyst=row["analyst"],
            identified_by=row["identified_by"],
            body=row["body"],
            events=tuple(
                RequestEvent(
                    id=int(e["id"]),
                    request=int(e["request"]),
                    at=_dt(e["at"]),
                    status=Status(e["status"]),
                    analyst=e["analyst"],
                    identified_by=e["identified_by"],
                    body=e["body"],
                )
                for e in events
            ),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _dt(value: int) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
