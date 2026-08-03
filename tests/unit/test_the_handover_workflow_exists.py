"""The commands the handover guide teaches must exist and round-trip.

`docs/handover.md` documented `bundle export` and `bundle import` while the CLI
only inspected. A reader following the guide got argparse's "invalid choice"
and no way to tell whether the workflow was missing or they had mistyped it.

This is the same defect class as the docstring tests, one level up: the docs
are prose describing a property the code did not have.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chainscope.cli.main import main

DOC = Path("docs/handover.md")


def _case(tmp_path: Path) -> Path:
    db = tmp_path / "case.db"
    conn = sqlite3.connect(db)
    conn.execute("create table notes (body text)")
    conn.execute("insert into notes values ('the reasoning nobody can rebuild')")
    conn.commit()
    conn.close()
    return db


def test_every_bundle_command_the_guide_names_is_real(capsys) -> None:
    """Read the verbs out of the guide rather than keeping a list by hand."""
    text = DOC.read_text()
    verbs = {v for v in ("export", "import") if f"bundle {v}" in text}
    assert verbs, "the guide stopped documenting bundle commands; update this test"
    for verb in verbs:
        with pytest.raises(SystemExit) as exit_info:
            main(["bundle", verb, "--help"])
        assert exit_info.value.code == 0, f"`bundle {verb}` is documented but absent"


def test_a_case_survives_the_round_trip(tmp_path: Path, capsys) -> None:
    """Export then import, and the irreplaceable half arrives."""
    case = _case(tmp_path)
    out = tmp_path / "bundle"
    code = main(
        [
            "bundle",
            "export",
            str(out),
            "--title",
            "Theft",
            "--case",
            str(case),
            "--cache",
            str(tmp_path / "absent.sqlite"),
            "--audit",
            str(tmp_path / "absent.jsonl"),
            "--archive",
            str(tmp_path / "case.zip"),
        ]
    )
    assert code == 0
    capsys.readouterr()

    main(["bundle", "import", str(tmp_path / "case.zip"), "--into", str(tmp_path / "in")])
    text = capsys.readouterr().out
    assert "Theft" in text
    # The notes must actually be there, not merely mentioned.
    restored = tmp_path / "in" / "case.db"
    assert restored.is_file()
    conn = sqlite3.connect(restored)
    assert conn.execute("select body from notes").fetchone()[0].startswith("the reasoning")
    conn.close()


def test_a_bundle_without_its_queries_says_so(tmp_path: Path, capsys) -> None:
    """Unverifiable must be loud, not inferred from a missing file."""
    main(
        [
            "bundle",
            "export",
            str(tmp_path / "b"),
            "--case",
            str(_case(tmp_path)),
            "--cache",
            str(tmp_path / "absent.sqlite"),
            "--audit",
            str(tmp_path / "absent.jsonl"),
        ]
    )
    assert "NOT replayable" in capsys.readouterr().out


def test_omitting_the_case_record_is_announced(tmp_path: Path, capsys) -> None:
    """--no-case is legitimate; a silent --no-case is not."""
    main(
        [
            "bundle",
            "export",
            str(tmp_path / "b"),
            "--no-case",
            "--cache",
            str(tmp_path / "absent.sqlite"),
            "--audit",
            str(tmp_path / "absent.jsonl"),
        ]
    )
    assert "NOT here" in capsys.readouterr().out


def test_the_documented_bare_form_still_works(tmp_path: Path, capsys) -> None:
    """The README's `chainscope bundle <path>` predates the subcommands."""
    main(
        [
            "bundle",
            "export",
            str(tmp_path / "b"),
            "--title",
            "Legacy",
            "--case",
            str(_case(tmp_path)),
            "--cache",
            str(tmp_path / "absent.sqlite"),
            "--audit",
            str(tmp_path / "absent.jsonl"),
        ]
    )
    capsys.readouterr()
    main(["bundle", str(tmp_path / "b")])
    assert "Legacy" in capsys.readouterr().out


def test_a_bundle_cannot_write_outside_where_it_is_unpacked(tmp_path: Path) -> None:
    """A zip is a list of names somebody else chose."""
    import zipfile

    from chainscope.case.bundle import Bundle, BundleError

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", '{"manifest_version": 1}')
        zf.writestr("../escaped.txt", "should never be written")
    with pytest.raises(BundleError, match="escapes"):
        Bundle.unpack(evil, tmp_path / "dest")
    assert not (tmp_path / "escaped.txt").exists()
