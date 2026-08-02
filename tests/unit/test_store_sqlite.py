"""The SQLite store's on-disk promises."""

from __future__ import annotations

from pathlib import Path

import pytest

from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.store.base import StoreError
from chainscope.store.sqlite import SqliteStore


class TestSchema4Migration:
    """Schema 3 stores predate `analyst`. They upgrade in place, losing nothing."""

    def _v3(self, path: Path) -> None:
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version','3');
            CREATE TABLE attributions (
                id INTEGER PRIMARY KEY, address TEXT NOT NULL, chain TEXT,
                label TEXT NOT NULL, category TEXT NOT NULL,
                confidence INTEGER NOT NULL, method TEXT NOT NULL,
                source TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
                observed_at INTEGER, UNIQUE (address, source, label));
            INSERT INTO attributions
                (address, chain, label, category, confidence, method, source,
                 rationale, observed_at)
            VALUES ('0xaa', 'eip155:1', 'Binance 14', 'cex', 3, 'label',
                    'etherscan', 'matches the published hot-wallet list',
                    1750000000);
            """
        )
        conn.commit()
        conn.close()

    def test_existing_claims_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        self._v3(path)
        store = SqliteStore(path)
        claims = store.attributions("0xaa")
        assert len(claims) == 1
        assert claims[0].label == "Binance 14"
        store.close()

    def test_migrated_rows_have_no_invented_author(self, tmp_path: Path) -> None:
        # Schema 3 recorded no authorship. Filling it in with whoever ran the
        # upgrade would attribute somebody else's claims to them.
        path = tmp_path / "old.db"
        self._v3(path)
        store = SqliteStore(path)
        assert store.attributions("0xaa")[0].analyst == ""
        store.close()

    def test_two_analysts_asserting_the_same_thing_are_two_records(
        self, tmp_path: Path
    ) -> None:
        # The bug the widened key fixes: identical (address, source, label) from
        # two people used to collapse, so the case said one person asserted what
        # two had.
        store = SqliteStore(tmp_path / "new.db")

        def by(analyst: str) -> Attribution:
            return Attribution(
                address="0xaa",
                chain=None,
                label="Binance 14",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LABEL,
                source="etherscan",
                analyst=analyst,
            )

        assert store.put_attributions([by("alice")]) == 1
        assert store.put_attributions([by("bob")]) == 1
        assert store.put_attributions([by("alice")]) == 0  # still idempotent
        assert len(store.attributions("0xaa")) == 2
        store.close()

    def test_a_newer_store_is_refused_rather_than_guessed_at(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "future.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('schema_version','99');"
        )
        conn.commit()
        conn.close()
        with pytest.raises(StoreError, match="newer chainscope"):
            SqliteStore(path)

    def test_a_failed_migration_leaves_the_store_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim the docstring makes, checked rather than asserted.

        `ALTER TABLE ... RENAME` runs in autocommit under Python's sqlite3 --- it
        does not open a transaction --- so without an explicit `BEGIN` the rename
        committed the moment it ran and `rollback()` could not undo it. That left
        a store with `attributions_v3`, no `attributions`, and a version still
        reading 3, so the next open retried the rename and failed on the name
        already existing. Unrecoverable without hand-editing the file.
        """
        import chainscope.store.sqlite as mod

        path = tmp_path / "old.db"
        self._v3(path)

        broken = (mod._ATTRIBUTIONS[0], "CREATE UNIQUE INDEX x ON attributions(nope)")
        monkeypatch.setattr(mod, "_ATTRIBUTIONS", broken)
        with pytest.raises(StoreError, match="rolled back"):
            SqliteStore(path)
        monkeypatch.undo()

        # Everything as it was, and the migration can simply be run again.
        store = SqliteStore(path)
        try:
            assert [c.label for c in store.attributions("0xaa")] == ["Binance 14"]
        finally:
            store.close()
