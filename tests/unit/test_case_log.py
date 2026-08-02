"""The case log's promises: append-only, authored, and honest about what is open."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chainscope.case.log import CaseLog, Identity, Note, NoteKind, whoami

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def note(kind: NoteKind = NoteKind.OBSERVATION, **kw: object) -> Note:
    fields: dict[str, object] = {
        "at": NOW,
        "analyst": "alice@lab",
        "identified_by": "env",
        "kind": kind,
        "body": "something happened",
    }
    fields.update(kw)
    return Note(**fields)  # type: ignore[arg-type]


class TestNote:
    def test_body_is_required(self) -> None:
        with pytest.raises(ValueError, match="needs a body"):
            note(body="   ")

    def test_correction_must_name_what_it_replaces(self) -> None:
        # Otherwise the log records that something was wrong and not what.
        with pytest.raises(ValueError, match="name the note it replaces"):
            note(NoteKind.CORRECTION)

    def test_only_a_correction_supersedes(self) -> None:
        with pytest.raises(ValueError, match="only a correction"):
            note(NoteKind.OBSERVATION, supersedes=1)


class TestCaseLog:
    def test_roundtrip_preserves_authorship(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        log.add(note(analyst="bob@lab", identified_by="git"))
        stored = log.notes()
        assert len(stored) == 1
        assert stored[0].analyst == "bob@lab"
        assert stored[0].identified_by == "git"
        log.close()

    def test_notes_come_back_in_working_order(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        log.add(note(at=NOW + timedelta(hours=2), body="second"))
        log.add(note(at=NOW, body="first"))
        assert [n.body for n in log.notes()] == ["first", "second"]
        log.close()

    def test_supersede_must_point_at_a_real_note(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        with pytest.raises(ValueError, match="no note 99"):
            log.add(note(NoteKind.CORRECTION, supersedes=99))
        log.close()

    def test_a_superseded_note_is_kept(self, tmp_path: object) -> None:
        # The whole design: "I thought X, then found Y" is the record. A log
        # that showed only the final position would be indistinguishable from
        # one that was right the first time.
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        first = log.add(note(body="the third hop is the thief"))
        log.add(note(NoteKind.CORRECTION, body="it is a router", supersedes=first))
        bodies = [n.body for n in log.notes()]
        assert "the third hop is the thief" in bodies
        assert log.superseded() == {first}
        log.close()

    def test_open_questions_exclude_answered_ones(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        asked = log.add(note(NoteKind.QUESTION, body="who paid the gas?"))
        still = log.add(note(NoteKind.QUESTION, body="where did it go?"))
        log.add(note(NoteKind.CORRECTION, body="the deployer did", supersedes=asked))

        open_ids = {n.id for n in log.open_questions()}
        assert open_ids == {still}
        log.close()

    def test_only_questions_count_as_open(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        log.add(note(NoteKind.OBSERVATION))
        log.add(note(NoteKind.DECISION))
        assert log.open_questions() == []
        log.close()

    def test_subject_is_lowercased_for_lookup(self, tmp_path: object) -> None:
        # Addresses arrive checksummed from one tool and lowercase from another;
        # a note filed under one casing must be findable by the other.
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        log.add(note(subject="0xAbCd"))
        assert len(log.notes(subject="0xabcd")) == 1
        assert len(log.notes(subject="0xABCD")) == 1
        log.close()

    def test_analysts_counts_each_person(self, tmp_path: object) -> None:
        log = CaseLog(tmp_path / "case.db")  # type: ignore[operator]
        log.add(note(analyst="alice@lab"))
        log.add(note(analyst="alice@lab"))
        log.add(note(analyst="bob@lab"))
        assert log.analysts() == [
            ("alice@lab", "env", 2),
            ("bob@lab", "env", 1),
        ]
        log.close()

    def test_survives_reopening(self, tmp_path: object) -> None:
        path = tmp_path / "case.db"  # type: ignore[operator]
        log = CaseLog(path)
        log.add(note(body="written before the crash"))
        log.close()

        again = CaseLog(path)
        assert [n.body for n in again.notes()] == ["written before the crash"]
        again.close()


class TestWhoami:
    def test_env_wins(self) -> None:
        who = whoami({"CHAINSCOPE_ANALYST": "carol@lab", "USER": "laptop"})
        assert who == Identity("carol@lab", "env")
        assert who.is_chosen

    def test_os_account_is_not_a_chosen_identity(self) -> None:
        # The distinction the whole field exists for: a machine login is not
        # authorship, and a report has to be able to say so.
        who = Identity("laptop", "os")
        assert not who.is_chosen
        assert "unverified" in str(who)

    def test_blank_env_falls_through(self) -> None:
        assert whoami({"CHAINSCOPE_ANALYST": "   "}).source != "env"
