"""Binding a figure to the queries that produced it.

The pieces were all here and nothing joined them: the audit log records every
query with its cache key, the cache holds the responses, and nothing said *this
number rests on these responses, and here is a hash of each*.

Without that, "the tool said so" is the whole provenance, which is a claim
about a claim.

**The fixture writes a real `Cache`.** It used to build a directory of JSON
files, because that is what the command assumed a cache was. The real one is a
single SQLite file with an `entries` table, so the command found nothing
against it: zero responses hashed, every query reported as uncached, and
`--verify` structurally unable to detect drift. It was a no-op in its own
documented workflow, and these tests passed the whole time --- because the
fixture had been built to match the assumption instead of the interface.

A fixture that agrees with the code about a shape neither has checked tests
nothing. Everything below now goes through `chainscope.transport.cache.Cache`.
"""

from __future__ import annotations

import json

import pytest

from chainscope.cli.main import main


@pytest.fixture
def case(tmp_path, monkeypatch):
    from chainscope.transport.cache import Cache, Volatility

    (tmp_path / ".chainscope").mkdir(parents=True)
    cache = Cache(tmp_path / ".chainscope/cache.db")
    cache.put("abc123", {"result": "0x1"}, Volatility.SETTLED, provider="etherscan")
    cache.put("def456", {"result": "0x2"}, Volatility.SETTLED, provider="rpc")
    (tmp_path / ".chainscope/audit.jsonl").write_text(
        '{"cache_key":"abc123","provider":"etherscan"}\n'
        '{"cache_key":"ghost","provider":"rpc"}\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _put(case, key, value):
    """Write a response through the same class the tool writes through."""
    from chainscope.transport.cache import Cache, Volatility

    Cache(case / ".chainscope/cache.db").put(key, value, Volatility.SETTLED)


class TestItRecordsWhatACaseRestsOn:
    def test_every_cached_response_is_hashed(self, case):
        assert main(["attest"]) == 0
        data = json.loads((case / ".chainscope/attestation.json").read_text())
        assert set(data["responses"]) == {"abc123", "def456"}
        assert len(data["responses"]["abc123"]["sha256"]) == 64

    def test_a_query_with_no_cached_response_is_named(self, case, capsys):
        """The ordinary case for anything uncacheable, and also what a pruned
        cache looks like. The difference matters when somebody asks what a
        figure rests on."""
        main(["attest"])
        data = json.loads((case / ".chainscope/attestation.json").read_text())
        assert data["queries_without_a_cached_response"] == ["ghost"]

    def test_an_unreadable_audit_line_is_counted(self, case):
        """A hole in the record is reported as one. Skipping silently would let
        a truncated log look complete."""
        (case / ".chainscope/audit.jsonl").write_text('{"cache_key":"abc123"}\n{bad\n')
        main(["attest"])
        data = json.loads((case / ".chainscope/attestation.json").read_text())
        assert data["unreadable_audit_lines"] == 1

    def test_it_says_it_is_not_a_signature(self, case, capsys):
        """A file called "attestation" implying tamper-proofing would be worse
        than none."""
        main(["attest"])
        out = capsys.readouterr().out
        assert "not a signature" in out
        assert "does not defend against anyone who can write" in out


class TestVerifyCatchesDrift:
    def test_an_unchanged_cache_verifies(self, case, capsys):
        main(["attest"])
        capsys.readouterr()
        assert main(["attest", "--verify"]) == 0
        assert "unchanged" in capsys.readouterr().out

    def test_a_changed_response_is_reported_and_fails(self, case, capsys):
        """The same query, a different answer, and no record of anyone deciding
        that."""
        main(["attest"])
        capsys.readouterr()
        _put(case, "abc123", {"result": "0xTAMPERED"})
        assert main(["attest", "--verify"]) == 1
        assert "CHANGED  abc123" in capsys.readouterr().out

    def test_a_deleted_response_is_reported_and_fails(self, case, capsys):
        main(["attest"])
        capsys.readouterr()
        import sqlite3

        conn = sqlite3.connect(case / ".chainscope/cache.db")
        conn.execute("DELETE FROM entries WHERE key = 'abc123'")
        conn.commit()
        conn.close()
        assert main(["attest", "--verify"]) == 1
        assert "MISSING  abc123" in capsys.readouterr().out

    def test_new_responses_do_not_fail_the_check(self, case, capsys):
        """Work continued. Reported so the count reconciles, not as drift."""
        main(["attest"])
        capsys.readouterr()
        _put(case, "new999", {})
        assert main(["attest", "--verify"]) == 0
        assert "new      new999" in capsys.readouterr().out

    def test_verifying_without_an_attestation_says_so(self, case, capsys):
        assert main(["attest", "--verify"]) == 2
        assert "to verify against" in capsys.readouterr().err


class TestItRefusesWhereThereIsNothing:
    def test_no_cache_and_no_log_is_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["attest"]) == 2
        err = capsys.readouterr().err
        assert "nothing to attest" in err
        # Names the env vars, because the usual cause is that neither was set.
        assert "CHAINSCOPE_AUDIT_LOG" in err

    def test_a_cache_holding_nothing_is_refused_rather_than_attested(
        self, tmp_path, monkeypatch, capsys
    ):
        """The exact shape the directory-scanning bug produced.

        An attestation over zero responses is a file that looks like provenance
        and binds nothing --- worse than refusing, because somebody would ship
        it. This is the check that would have caught the original defect.
        """
        from chainscope.transport.cache import Cache

        (tmp_path / ".chainscope").mkdir()
        Cache(tmp_path / ".chainscope/cache.db")._db()
        (tmp_path / ".chainscope/audit.jsonl").write_text('{"cache_key":"q1"}\n')
        monkeypatch.chdir(tmp_path)

        assert main(["attest"]) == 2
        assert "binds nothing" in capsys.readouterr().err
        assert not (tmp_path / ".chainscope/attestation.json").exists()

    def test_a_directory_is_refused_with_the_reason(self, case, capsys):
        # What this command used to assume a cache was. Saying so beats
        # returning zero entries and calling it an attestation.
        assert main(["attest", "--cache", ".chainscope"]) == 2
        assert "is a directory" in capsys.readouterr().err

    def test_the_hash_is_of_the_stored_bytes(self, case):
        """Not of a parsed form: a parser that changed how it renders a field
        would otherwise look like tampering, and a real change the parser
        normalises away would not show up at all."""
        import hashlib
        import json as _json
        import sqlite3

        from chainscope.cli.commands.attest import _responses

        conn = sqlite3.connect(case / ".chainscope/cache.db")
        stored = conn.execute("SELECT value FROM entries WHERE key = 'abc123'").fetchone()[0]
        conn.close()

        got = _responses(case / ".chainscope/cache.db")["abc123"]
        assert got["sha256"] == hashlib.sha256(stored.encode()).hexdigest()
        # And it really is what the cache stored, not something re-serialised
        # on the way through.
        assert _json.loads(stored) == {"result": "0x1"}


class TestCompare:
    """The verification decision, asserted directly rather than through stdout.

    Reading it back out of printed text tests the wording, and the wording is
    the part that is allowed to change.
    """

    def _sha(self, value: str) -> dict[str, dict[str, object]]:
        return {k: {"sha256": v} for k, v in (pair.split("=") for pair in value.split())}

    def test_a_moved_response_is_drift(self) -> None:
        from chainscope.cli.commands.attest import compare

        out = compare(self._sha("a=1 b=2"), self._sha("a=1 b=9"))
        assert out["changed"] == ["b"]
        assert out["unchanged"] == ["a"]

    def test_a_deleted_response_is_drift(self) -> None:
        from chainscope.cli.commands.attest import compare

        assert compare(self._sha("a=1"), self._sha(""))["gone"] == ["a"]

    def test_a_new_response_is_not_drift(self) -> None:
        # Work continued. Reported so the count reconciles, not as tampering.
        from chainscope.cli.commands.attest import compare

        out = compare(self._sha("a=1"), self._sha("a=1 b=2"))
        assert out["new"] == ["b"]
        assert not out["changed"] and not out["gone"]

    def test_holes_in_the_record_travel_with_the_result(self) -> None:
        from chainscope.cli.commands.attest import compare

        out = compare(self._sha("a=1"), self._sha("a=1"), uncached=["q1"], unreadable=3)
        assert out["uncached"] == ["q1"]
        assert out["unreadable_audit_lines"] == 3


class TestVerifyReportsHoles:
    def test_uncached_and_unreadable_are_printed_on_a_clean_run(self, tmp_path, capsys) -> None:
        """They were passed to `_verify` and never used.

        A verify that printed "unchanged" while staying silent about holes in
        the audit log answers a narrower question than the one being asked.
        """
        from chainscope.cli.commands.attest import _verify

        out = tmp_path / "attestation.json"
        out.write_text('{"responses": {}}')
        code = _verify(out, {}, ["q1", "q2"], 4)
        text = capsys.readouterr().out
        assert "2 quer(ies) have no cached response" in text
        assert "4 unreadable audit line(s)" in text
        # Still zero: a gap in what was recorded is not evidence that a
        # recorded response moved, and one exit code cannot answer both.
        assert code == 0


class TestTheAttestationIsWrittenAtomically:
    def test_no_partial_file_is_left_behind(self, case):
        main(["attest"])
        leftovers = list((case / ".chainscope").glob("*.partial"))
        assert leftovers == []

    def test_an_unreadable_attestation_is_refused_not_ignored(self, case, capsys):
        """A truncated file must not verify as "nothing to compare".

        That is the shape a crashed write leaves, and treating it as empty
        would report a clean check against no baseline at all.
        """
        main(["attest"])
        capsys.readouterr()
        (case / ".chainscope/attestation.json").write_text('{"responses": {')
        assert main(["attest", "--verify"]) == 2
        assert "is not readable JSON" in capsys.readouterr().err
