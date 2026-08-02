"""Cassette record and replay.

The behaviour worth guarding hardest is the refusal to write. A cassette is
committed to a public repository, so a credential that reaches one is a
disclosed credential --- and the failure has to be loud at the moment of
writing, not discovered later in a scrollback buffer.
"""

import json

import pytest

from chainscope.transport.cache import Volatility
from chainscope.transport.cassette import (
    FORMAT_VERSION,
    Cassette,
    CassetteError,
    CredentialLeak,
    Mode,
)
from chainscope.transport.credentials import forget_secret, register_secret


@pytest.fixture
def path(tmp_path):
    return tmp_path / "fixture.json"


class TestModes:
    def test_record_writes_and_replay_reads(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", {"status": "1"}, Volatility.SLOW, provider="etherscan")
        assert Cassette(path).get("k1", Volatility.SLOW) == {"status": "1"}

    def test_replay_never_writes(self, path):
        Cassette(path, mode=Mode.RECORD).put("k1", 1, Volatility.SLOW)
        c = Cassette(path, mode=Mode.REPLAY)
        c.put("k2", 2, Volatility.SLOW)
        assert c.save() is False
        assert "k2" not in c

    def test_record_ignores_existing_entries(self, path):
        """So a re-record actually re-fetches instead of replaying itself."""
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "old", Volatility.SLOW)
        assert Cassette(path, mode=Mode.RECORD).get("k1", Volatility.SLOW) is None

    def test_once_replays_what_exists(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "old", Volatility.SLOW)
        assert Cassette(path, mode=Mode.ONCE).get("k1", Volatility.SLOW) == "old"

    def test_once_appends_what_does_not(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "old", Volatility.SLOW)
        with Cassette(path, mode=Mode.ONCE) as c:
            c.put("k2", "new", Volatility.SLOW)
        replayed = Cassette(path)
        assert replayed.get("k1", Volatility.SLOW) == "old"
        assert replayed.get("k2", Volatility.SLOW) == "new"

    def test_unknown_mode_is_rejected(self, path):
        with pytest.raises(CassetteError, match="unknown mode"):
            Cassette(path, mode="sideways")


class TestExpiry:
    def test_recordings_do_not_expire(self, path):
        """A test that passes today and fails next Tuesday for no reason
        connected to the code is worse than no test."""
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "v", Volatility.HEAD)
        assert Cassette(path).get("k1", Volatility.HEAD) == "v"

    def test_volatility_never_is_still_served(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "v", Volatility.NEVER)
        assert Cassette(path).get("k1", Volatility.NEVER) == "v"


class TestCredentialRefusal:
    def test_a_credential_registered_after_recording_stops_the_write(self, path):
        """The gap the save-time check exists to close.

        Scrubbing on the way in handles everything known at the time of
        recording. It cannot handle a value that only becomes recognisable as a
        credential afterwards --- a cassette recorded before a key was loaded,
        or entries read back from an existing file, which are not re-scrubbed.
        """
        secret = "live-key-do-not-commit-0000"
        c = Cassette(path, mode=Mode.RECORD)
        c.put("k1", {"message": f"invalid key {secret}"}, Volatility.SLOW)

        register_secret(secret)
        try:
            with pytest.raises(CredentialLeak, match="Nothing"):
                c.save()
            assert not path.exists()
        finally:
            forget_secret(secret)

    def test_a_credential_in_a_free_text_value_is_scrubbed_on_the_way_in(self, path):
        """The structural pass reaches inside strings, not just known parameter
        names, so the common case never gets as far as the save-time check."""
        secret = "live-key-do-not-commit-1111"
        register_secret(secret)
        try:
            with Cassette(path, mode=Mode.RECORD) as c:
                c.put("k1", {"message": f"invalid key {secret}"}, Volatility.SLOW)
            assert secret not in path.read_text()
        finally:
            forget_secret(secret)

    def test_structural_scrubbing_happens_on_the_way_in(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", {"apikey": "aaaaaaaaaaaaaaaaaaaa"}, Volatility.SLOW)
        assert "aaaaaaaaaaaaaaaaaaaa" not in path.read_text()

    def test_a_clean_cassette_writes_normally(self, path):
        register_secret("some-unrelated-secret-value")
        try:
            with Cassette(path, mode=Mode.RECORD) as c:
                c.put("k1", {"result": "0xdeadbeef"}, Volatility.SLOW)
            assert path.exists()
        finally:
            forget_secret("some-unrelated-secret-value")


class TestFile:
    def test_format_version_is_checked(self, path):
        path.write_text(json.dumps({"version": 99, "interactions": []}))
        with pytest.raises(CassetteError, match="Re-record"):
            Cassette(path)

    def test_malformed_json_is_rejected(self, path):
        path.write_text("{not json")
        with pytest.raises(CassetteError):
            Cassette(path)

    def test_missing_file_is_an_empty_cassette(self, path):
        assert len(Cassette(path)) == 0

    def test_labels_survive_the_round_trip(self, path):
        """A cassette of bare hashes cannot be reviewed, and an unreviewable
        fixture gets approved on trust."""
        with Cassette(path, mode=Mode.RECORD) as c, c.labelling("txlist: Ronin exploiter"):
            c.put("k1", "v", Volatility.SLOW)
        assert Cassette(path).labels() == ["txlist: Ronin exploiter"]

    def test_labels_are_scoped_to_the_block(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            with c.labelling("inside"):
                c.put("k1", "v", Volatility.SLOW)
            c.put("k2", "v", Volatility.SLOW)
        assert Cassette(path).labels() == ["inside", "(unlabelled)"]

    def test_recording_order_is_preserved(self, path):
        """So a diff reads as a sequence rather than a reshuffle."""
        with Cassette(path, mode=Mode.RECORD) as c:
            for i in range(5):
                c.put(f"k{i}", i, Volatility.SLOW)
        assert [i.key for i in Cassette(path).interactions()] == [f"k{i}" for i in range(5)]

    def test_written_file_declares_its_version(self, path):
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "v", Volatility.SLOW)
        assert json.loads(path.read_text())["version"] == FORMAT_VERSION

    def test_a_failed_block_does_not_save(self, path):
        """A partial recording committed as though complete is worse than none."""
        with pytest.raises(RuntimeError), Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "v", Volatility.SLOW)
            raise RuntimeError("recording blew up halfway")
        assert not path.exists()

    def test_an_interrupted_save_leaves_the_previous_file(self, path):
        """Write-then-rename: a truncated cassette fails to parse forever."""
        with Cassette(path, mode=Mode.RECORD) as c:
            c.put("k1", "first", Volatility.SLOW)
        original = path.read_text()

        c = Cassette(path, mode=Mode.RECORD)
        c.put("k2", "second", Volatility.SLOW)
        register_secret("planted-credential-value")
        try:
            c._entries["k2"].response = "planted-credential-value"
            with pytest.raises(CredentialLeak):
                c.save()
        finally:
            forget_secret("planted-credential-value")
        assert path.read_text() == original
