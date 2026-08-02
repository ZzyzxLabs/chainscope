"""The case log: what a person concluded, when, and why.

Every claim in this package carries a source. Nothing carried a *narrative* ---
the reasoning that connects one claim to the next, the thing that was ruled out
at four in the afternoon and never written down, the question somebody asked
that nobody has answered yet. `Attribution.rationale` explains one label. There
was nowhere to explain an investigation.

**It is a separate file from the store, and that is the whole design.** The
store is derived and disposable by construction: :mod:`chainscope.store.base`
promises it can be rebuilt from the cache, `clear()` exists, and a schema change
is meant to be a rebuild rather than a migration. None of that is true of what a
person wrote down. A narrative that a routine rebuild can destroy is a narrative
nobody will commit anything important to, so this lives in ``case.db`` and the
store never touches it.

**Append-only.** There is no edit and no delete. A note that was wrong is
superseded by a `correction` naming the note it replaces, and both stay
readable --- because "I thought X, then found Y" is the substance of an
investigation, and a log that silently reflects only the final position cannot
be told apart from one that was right the first time. If somebody genuinely
needs to remove a line, they can open the SQLite file and do it deliberately;
this will not do it for them.

**A question is a first-class kind.** Most of what an investigation contains at
any moment is unanswered, and a record that holds only conclusions reads as more
complete than it is. :meth:`CaseLog.open_questions` is the part of a report that
says what is still not known.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

__all__ = ["CaseLog", "Identity", "Note", "NoteKind", "whoami"]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY,
    at            INTEGER NOT NULL,
    analyst       TEXT NOT NULL,
    -- How the analyst name was arrived at. An identity somebody chose and one
    -- the machine guessed from the OS account are not the same claim about
    -- authorship, and a shared case has to be able to tell them apart.
    identified_by TEXT NOT NULL,
    kind          TEXT NOT NULL,
    body          TEXT NOT NULL,
    -- The address or transaction this attaches to. Empty means the note is
    -- about the case rather than about a thing in it, which is most of the
    -- reasoning worth keeping.
    subject       TEXT NOT NULL DEFAULT '',
    chain         TEXT,
    supersedes    INTEGER REFERENCES notes(id)
);

CREATE INDEX IF NOT EXISTS ix_notes_at      ON notes(at);
CREATE INDEX IF NOT EXISTS ix_notes_subject ON notes(subject);
"""


class NoteKind(Enum):
    """What kind of statement a note is.

    Four, not free text. A narrative where everything is one undifferentiated
    kind cannot answer the two questions a report needs to ask of it --- what is
    still open, and what has been withdrawn.
    """

    OBSERVATION = "observation"
    """Something seen. The raw material."""

    DECISION = "decision"
    """A choice made and the reason for it --- a line of enquiry dropped, a
    threshold chosen, a lead judged not worth following. The kind that is
    always reconstructed badly afterwards."""

    QUESTION = "question"
    """Open until something supersedes it. Counted in a report as unfinished."""

    CORRECTION = "correction"
    """Replaces an earlier note. Requires the id of the note it replaces:
    without it, "that was wrong" names nothing and the log is worse than if
    the correction had not been filed."""


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is writing, and how that was worked out."""

    name: str
    source: str
    """``env``, ``git``, or ``os``."""

    @property
    def is_chosen(self) -> bool:
        """Whether a person stated this identity rather than the machine inferring it.

        A report shows the difference. ``os`` is a local account name that may
        mean nothing on another machine and belong to nobody in particular; a
        case shared between analysts needs that visible, not smoothed over.
        """
        return self.source in ("env", "git")

    def __str__(self) -> str:
        return self.name if self.is_chosen else f"{self.name} (OS account, unverified)"


def whoami(env: dict[str, str] | None = None) -> Identity:
    """Resolve the current analyst.

    Order: ``CHAINSCOPE_ANALYST``, then git's configured e-mail, then the OS
    account. The fallback exists so a cold start is not a configuration task,
    and :attr:`Identity.source` is carried alongside so nothing has to pretend
    the last one is an identity somebody claimed.
    """
    environ = os.environ if env is None else env
    stated = environ.get("CHAINSCOPE_ANALYST", "").strip()
    if stated:
        return Identity(stated, "env")

    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Identity(out.stdout.strip(), "git")
    except (OSError, subprocess.SubprocessError):
        # No git, or it hung. Not worth reporting: the fallback below is fine
        # and the identity source records that this is not a chosen name.
        pass

    return Identity(environ.get("USER") or environ.get("USERNAME") or "unknown", "os")


def _must_be_aware(value: datetime, what: str) -> None:
    """Refuse a naive datetime.

    `datetime.timestamp()` reads a naive value as *local* time, so a note
    recorded at 12:00 without a zone is stored eight hours off on a machine in
    UTC+8 and reads back as 04:00 --- silently, and differently on the
    colleague's machine that opens the same case file. *When* is regularly the
    fact in dispute in a case record, so the one thing this must not do is
    guess a zone.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{what} has no timezone. A naive datetime is read as local time, "
            f"which stores a different instant on every machine --- pass one "
            f"with tzinfo (datetime.now(timezone.utc))"
        )


@dataclass(frozen=True, slots=True)
class Note:
    """One line of the case narrative."""

    at: datetime
    analyst: str
    identified_by: str
    kind: NoteKind
    body: str
    subject: str = ""
    chain: str | None = None
    supersedes: int | None = None
    id: int = 0

    def __post_init__(self) -> None:
        _must_be_aware(self.at, "a note's timestamp")
        if not self.body.strip():
            raise ValueError("a note needs a body")
        if self.kind is NoteKind.CORRECTION and not self.supersedes:
            raise ValueError(
                "a correction has to name the note it replaces (--supersedes N). "
                "Without it the log records that something was wrong and not what"
            )
        if self.kind is not NoteKind.CORRECTION and self.supersedes:
            raise ValueError(
                f"only a correction supersedes an earlier note; this is a "
                f"{self.kind.value}. Record it as a correction, or drop --supersedes"
            )

    @property
    def is_open_question(self) -> bool:
        return self.kind is NoteKind.QUESTION


class CaseLog:
    """Append-only narrative for one case."""

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
        if self.path:
            # A reader is not blocked by a writer: `report` and `graph` read
            # this file while somebody may be adding a note to it.
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self._conn.commit()

    def add(self, note: Note) -> int:
        """Append a note. Returns its id, which a later correction can name."""
        if note.supersedes is not None and not self._exists(note.supersedes):
            # Checked here rather than left to the foreign key, so the message
            # says what is wrong instead of "FOREIGN KEY constraint failed".
            raise ValueError(f"no note {note.supersedes} in {self.path} to supersede")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO notes "
                "(at, analyst, identified_by, kind, body, subject, chain, supersedes) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    int(note.at.timestamp()),
                    note.analyst,
                    note.identified_by,
                    note.kind.value,
                    note.body,
                    note.subject.lower(),
                    note.chain,
                    note.supersedes,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def _exists(self, note_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
        return row is not None

    def notes(self, *, subject: str = "", analyst: str = "") -> list[Note]:
        """Every note, oldest first --- the order somebody worked in."""
        sql = "SELECT * FROM notes"
        where, params = [], []
        if subject:
            where.append("subject = ?")
            params.append(subject.lower())
        if analyst:
            where.append("analyst = ?")
            params.append(analyst)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY at, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_to_note(r) for r in rows]

    def superseded(self) -> set[int]:
        """Ids that some later correction replaced."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT supersedes FROM notes WHERE supersedes IS NOT NULL"
            ).fetchall()
        return {int(r["supersedes"]) for r in rows}

    def open_questions(self) -> list[Note]:
        """Questions nothing has superseded.

        The part of a report that says what is not known. A case record listing
        only what was concluded reads as finished; this is the counterweight,
        and it is why `question` is a kind rather than a note somebody typed the
        word "TODO" into.
        """
        replaced = self.superseded()
        return [n for n in self.notes() if n.kind is NoteKind.QUESTION and n.id not in replaced]

    def analysts(self) -> list[tuple[str, str, int]]:
        """``(name, how it was identified, note count)``, busiest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT analyst, identified_by, COUNT(*) n FROM notes "
                "GROUP BY analyst, identified_by ORDER BY n DESC, analyst"
            ).fetchall()
        return [(r["analyst"], r["identified_by"], int(r["n"])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> CaseLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=int(row["id"]),
        at=datetime.fromtimestamp(int(row["at"]), tz=timezone.utc),
        analyst=row["analyst"],
        identified_by=row["identified_by"],
        kind=NoteKind(row["kind"]),
        body=row["body"],
        subject=row["subject"] or "",
        chain=row["chain"],
        supersedes=int(row["supersedes"]) if row["supersedes"] is not None else None,
    )
