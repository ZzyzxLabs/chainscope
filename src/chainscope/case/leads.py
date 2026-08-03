"""Where leads live between the moment somebody finds one and the moment it is settled.

:mod:`chainscope.osint.leads` defines what a lead *is* --- something to look
into, kept rigorously apart from anything concluded, and carrying the specific
step that would confirm it. It has been in this package since early on with
**no callers whatsoever**: nothing produced a lead, nothing stored one, and no
command showed one. §2 of `docs/needs.md` names that failure directly --- a
technique nobody can reach does not exist --- and this module is the other half
of it.

**Leads belong in the case record, not the store.** ``store.db`` is derived and
disposable; it is rebuilt from the cache and nobody would mourn it. A lead is
somebody's *work* --- a handle they noticed, a domain they matched --- and it
cannot be recomputed from chain data because it did not come from chain data.
So it sits in ``case.db`` beside the notes and the correspondence ledger, all
of which share that property.

**Settled is a separate column from what was found, and both are kept.** The
temptation is to delete a lead once it is refuted, which throws away the most
valuable thing in an investigation: the record that somebody already checked.
Without it the next analyst re-runs the same search, and in a shared case two
people do it in parallel. A refuted lead is a completed piece of work.

**A verdict cannot be recorded without a reason.** ``confirmed`` and ``refuted``
are claims about the outside world --- that @alice does or does not control this
address --- and a claim with no stated basis is indistinguishable in the record
from a guess somebody made quickly. The reason is the difference between a
finding and an opinion, and it is required in the type.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ..chains import fold_if_hex
from ..osint.leads import Lead

__all__ = ["LeadRecord", "LeadStore", "Verdict"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY,
    at         INTEGER NOT NULL,
    analyst    TEXT NOT NULL,
    address    TEXT NOT NULL,
    chain      TEXT,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    asserted_by TEXT NOT NULL,
    -- Required by `Lead` itself, and carried through rather than regenerated:
    -- the step that would settle this, written when the lead was found by
    -- whoever understood why it mattered.
    verify_by  TEXT NOT NULL,
    verdict    TEXT NOT NULL DEFAULT 'open',
    -- Why the verdict was reached. Empty only while the verdict is 'open';
    -- `settle` refuses to record one without it.
    reason     TEXT NOT NULL DEFAULT '',
    settled_at INTEGER,
    settled_by TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_leads_address ON leads(address);
CREATE INDEX IF NOT EXISTS ix_leads_verdict ON leads(verdict);
-- One lead per (chain, address, kind, value): the same handle found twice by
-- two analysts is one lead with two finders, not two leads. Without this a
-- rerun of the same enumeration doubles every count in the report.
--
-- `chain` is in the key because it was not, and the same hex address on
-- Ethereum and on BSC collapsed into a single lead --- they are different
-- accounts controlled by different people, and a verdict reached about one was
-- silently presented as settling the other. COALESCE because a chainless lead
-- is legitimate and NULL never equals NULL in an index.
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_identity
    ON leads(COALESCE(chain, ''), address, kind, value);
"""


def _fold(address: str) -> str:
    """Normalise an address without knowing its chain.

    `fold_if_hex` folds only what is unambiguously a 42-character `0x` hex
    string and returns everything else exactly as given. `.lower()` here would
    be right on EVM and would destroy a base58 or bech32 address --- silently,
    by making two different addresses compare equal.
    """
    return fold_if_hex(address.strip())


class Verdict(str, Enum):
    """What happened to a lead.

    A `str` enum with no ordering. These are not degrees of anything: a refuted
    lead is not "less confirmed", it is a different and equally finished piece
    of work.
    """

    OPEN = "open"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNREACHABLE = "unreachable"
    """Checked, and the check could not be completed --- the account was
    deleted, the site is gone. Distinct from `refuted`, which says the claim is
    false, and from `open`, which says nobody has looked. Collapsing it into
    either loses the fact that somebody spent the time."""


@dataclass(frozen=True, slots=True)
class LeadRecord:
    """A stored lead, with whatever became of it."""

    id: int
    at: datetime
    analyst: str
    address: str
    kind: str
    value: str
    source: str
    asserted_by: str
    verify_by: str
    verdict: Verdict = Verdict.OPEN
    reason: str = ""
    settled_at: datetime | None = None
    settled_by: str = ""
    chain: str | None = None

    @property
    def is_open(self) -> bool:
        return self.verdict == Verdict.OPEN

    def __str__(self) -> str:
        head = f"[{self.id}] {self.kind}: {self.value}"
        if self.is_open:
            return f"{head}  (open --- {self.verify_by})"
        return f"{head}  ({self.verdict.value} by {self.settled_by}: {self.reason})"


class LeadStore:
    """Leads for one case, in the case database.

    Not in ``store.db``. That file is derived from the cache and can be thrown
    away; a lead is somebody's work and cannot be recomputed, because it did not
    come from chain data in the first place.
    """

    def __init__(self, path: Path | str = ".chainscope/case.db") -> None:
        self.path = Path(path) if path != ":memory:" else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path) if self.path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        # WAL so a long-running reader --- a dashboard, a watching agent --- does
        # not block somebody filing a lead.
        if self.path:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LeadStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(self, lead: Lead, analyst: str, chain: str | None = None) -> tuple[int, bool]:
        """File a lead. Returns ``(id, was_new)``.

        ``was_new`` is returned rather than left for the caller to infer,
        because the caller cannot: the id looks identical either way. Reporting
        a re-filing as a fresh success hides the single most useful thing this
        can say to somebody about to spend an hour --- that it is already known,
        and possibly already refuted.

        Filing the same lead twice is not an error and does not duplicate: two
        analysts finding the same handle have found one thing. What the second
        filing must not do is silently discard an existing *verdict*, so this
        never touches a row that already exists --- re-filing a refuted lead
        leaves it refuted, and the reason somebody wrote stays.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM leads WHERE COALESCE(chain, '') = ? "
                "AND address = ? AND kind = ? AND value = ?",
                (chain or "", _fold(lead.address), lead.kind, lead.value),
            ).fetchone()
            if row is not None:
                return int(row["id"]), False
            cursor = self._conn.execute(
                "INSERT INTO leads (at, analyst, address, chain, kind, value, source, "
                "asserted_by, verify_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    int(datetime.now(timezone.utc).timestamp()),
                    analyst,
                    _fold(lead.address),
                    chain,
                    lead.kind,
                    lead.value,
                    lead.source,
                    lead.asserted_by,
                    lead.verify_by,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0), True

    def settle(self, lead_id: int, verdict: Verdict, reason: str, analyst: str) -> LeadRecord:
        """Record what checking a lead found.

        ``reason`` is required, and the refusal is the point. "Confirmed" and
        "refuted" are claims about the world outside this tool --- that somebody
        does or does not control an address --- and one with no stated basis
        reads in the record exactly like one somebody guessed at speed. The
        reason is what makes it re-checkable by the next person.
        """
        if not reason.strip():
            raise ValueError(
                "settling a lead needs a reason. 'Confirmed' with no stated basis "
                "is indistinguishable from a guess once the person who wrote it "
                "has moved on, and this record outlives them"
            )
        if verdict == Verdict.OPEN:
            raise ValueError(
                "a lead cannot be settled as 'open'. To reopen one, file the "
                "correction as a note --- the record of who checked and what they "
                "found is the most valuable thing here and is not overwritten"
            )
        with self._lock:
            found = self._conn.execute(
                "SELECT id, verdict, reason, settled_by FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if found is None:
                raise ValueError(f"no lead {lead_id}")
            if str(found["verdict"]) != Verdict.OPEN.value:
                # This module's own docstring says the record that somebody
                # checked is the most valuable thing here, and then `settle`
                # overwrote it: a second call replaced the first analyst's
                # verdict, their reason, and their name, with no trace. A
                # disagreement is a real event and it is a *note*, which is
                # append-only; it is not a silent edit of somebody else's work.
                raise ValueError(
                    f"lead {lead_id} was already settled as "
                    f"{found['verdict']} by {found['settled_by']}: "
                    f"{found['reason']}. Record a disagreement as a note rather "
                    f"than overwriting what they found"
                )
            self._conn.execute(
                "UPDATE leads SET verdict = ?, reason = ?, settled_at = ?, settled_by = ? "
                "WHERE id = ?",
                (
                    verdict.value,
                    reason.strip(),
                    int(datetime.now(timezone.utc).timestamp()),
                    analyst,
                    lead_id,
                ),
            )
            self._conn.commit()
        return self.get(lead_id)

    def get(self, lead_id: int) -> LeadRecord:
        row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            raise ValueError(f"no lead {lead_id}")
        return _to_record(row)

    def leads(
        self, address: str | None = None, verdict: Verdict | None = None
    ) -> list[LeadRecord]:
        """Leads, newest last, filtered by address or verdict.

        Refuted leads come back by default. Hiding them would lose the record
        that somebody already checked, which is what stops the next analyst
        repeating the search --- and in a shared case, stops two people doing it
        at the same time.
        """
        where, params = ["1=1"], []
        if address:
            where.append("address = ?")
            params.append(_fold(address))
        if verdict is not None:
            where.append("verdict = ?")
            params.append(verdict.value)
        rows = self._conn.execute(
            f"SELECT * FROM leads WHERE {' AND '.join(where)} ORDER BY at, id", params
        ).fetchall()
        return [_to_record(r) for r in rows]

    def open_leads(self, address: str | None = None) -> list[LeadRecord]:
        """What is still worth somebody's time."""
        return self.leads(address, Verdict.OPEN)

    def summary(self) -> dict[str, int]:
        """Counts by verdict, including the ones that are zero.

        Every verdict appears whether or not it occurred, so a reader can tell
        "nothing was refuted" from "the refuted category does not exist here".
        """
        counts = {v.value: 0 for v in Verdict}
        for row in self._conn.execute("SELECT verdict, COUNT(*) n FROM leads GROUP BY verdict"):
            counts[str(row["verdict"])] = int(row["n"])
        return counts


def _to_record(row: sqlite3.Row) -> LeadRecord:
    settled = row["settled_at"]
    return LeadRecord(
        id=int(row["id"]),
        at=datetime.fromtimestamp(int(row["at"]), tz=timezone.utc),
        analyst=str(row["analyst"]),
        address=str(row["address"]),
        chain=row["chain"],
        kind=str(row["kind"]),
        value=str(row["value"]),
        source=str(row["source"]),
        asserted_by=str(row["asserted_by"]),
        verify_by=str(row["verify_by"]),
        verdict=Verdict(str(row["verdict"])),
        reason=str(row["reason"]),
        settled_at=(
            datetime.fromtimestamp(int(settled), tz=timezone.utc)
            if settled is not None
            else None
        ),
        settled_by=str(row["settled_by"]),
    )
