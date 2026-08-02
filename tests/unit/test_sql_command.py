"""`chainscope sql`: the Dune-style surface, and what it refuses.

The analytics view existed and only agents could reach it. A person at a
terminal had no way to ask anything the fixed subcommands do not answer, which
is most questions an investigation actually asks.

The guard is tested in tests/unit/test_sql_guard.py. This is about the command:
that a refusal is a refusal and not a traceback, that a capped result says it
was capped, and that NULL is distinguishable from empty --- in this schema NULL
means something specific, since no asset is the native coin and no chain is
every chain.
"""

from __future__ import annotations

import json

import pytest

from chainscope.cli.main import main
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

ETH = 10**18
A = "0x" + "a" * 40


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "case"
    (root / ".chainscope").mkdir(parents=True)
    store = SqliteStore(root / ".chainscope/store.db")
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, A, A),
                recipient=Address(ETHEREUM, f"0x{i:040x}", f"0x{i:040x}"),
                # 100 ETH is 1e20, well past SQLite's 64-bit INTEGER. Summing
                # these is the reason the DuckDB layer exists.
                amount=Amount((i + 1) * 100 * ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=100 + i,
                index=0,
            )
            for i in range(5)
        ],
        source="t",
    )
    store.put_attributions(
        [
            Attribution(
                label="Tornado 10 ETH",
                category=Category.MIXER,
                confidence=Confidence.HIGH,
                method=Method.LIST,
                source="sanctions list",
                address=f"0x{3:040x}",
                chain=ETHEREUM,
            )
        ]
    )
    store.close()
    monkeypatch.chdir(root)
    return root


class TestItAnswersQuestions:
    def test_a_simple_aggregate(self, case, capsys):
        assert main(["sql", "SELECT count(*) AS n FROM transfers"]) == 0
        assert "5" in capsys.readouterr().out

    def test_exact_arithmetic_past_sixty_four_bits(self, case, capsys):
        """1500 ETH is 1.5e21 wei. A 64-bit sum would have wrapped."""
        main(["sql", "SELECT SUM(amount_raw) AS total FROM transfers", "-O", "json"])
        out = json.loads(capsys.readouterr().out)
        assert out[0]["total"] == str(1500 * ETH)

    def test_a_join_against_labels(self, case, capsys):
        main(
            [
                "sql",
                "SELECT a.label FROM transfers t JOIN attributions a "
                "ON a.address = t.recipient",
            ]
        )
        assert "Tornado 10 ETH" in capsys.readouterr().out

    def test_schema_lists_the_tables_and_the_traps(self, capsys):
        assert main(["sql", "--schema"]) == 0
        out = capsys.readouterr().out
        assert "transfers" in out and "attributions" in out
        # The two things a reader gets wrong without being told.
        assert "never by `symbol`" in out
        assert "do not compare across assets" in out

    def test_csv_output_is_pipeable(self, case, capsys):
        main(["sql", "SELECT count(*) AS n FROM transfers", "-O", "csv"])
        assert capsys.readouterr().out.strip().splitlines() == ["n", "5"]


class TestItSaysWhenItIsIncomplete:
    def test_a_capped_result_is_reported_and_exits_nonzero(self, case, capsys):
        """A query returning exactly `limit` rows looks the same whether that
        was the answer or the cap."""
        assert main(["sql", "SELECT * FROM transfers", "-n", "2"]) == 1
        assert "lower bound" in capsys.readouterr().err

    def test_an_uncapped_result_exits_zero(self, case, capsys):
        assert main(["sql", "SELECT * FROM transfers", "-n", "50"]) == 0
        assert "lower bound" not in capsys.readouterr().err

    def test_null_is_not_rendered_as_empty(self, case, capsys):
        """NULL means the native coin here; blank would read as a missing
        value."""
        main(["sql", "SELECT asset FROM transfers LIMIT 1"])
        assert "NULL" in capsys.readouterr().out


class TestItRefusesRatherThanCrashing:
    def test_a_write_is_refused_with_an_explanation(self, case, capsys):
        assert main(["sql", "DELETE FROM transfers"]) == 2
        assert "refused" in capsys.readouterr().err

    def test_chained_statements_are_refused(self, case, capsys):
        assert main(["sql", "SELECT '--' ; DROP TABLE transfers"]) == 2
        assert "refused" in capsys.readouterr().err

    def test_the_table_still_exists_afterwards(self, case, capsys):
        main(["sql", "SELECT '--' ; DROP TABLE transfers"])
        capsys.readouterr()
        assert main(["sql", "SELECT count(*) FROM transfers"]) == 0

    def test_a_broken_query_is_an_error_not_a_traceback(self, case, capsys):
        assert main(["sql", "SELECT nope FROM transfers"]) == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_no_store_says_so(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["sql", "SELECT 1"]) == 1
        assert "no store" in capsys.readouterr().err

    def test_no_query_points_at_the_schema(self, case, capsys):
        assert main(["sql"]) == 2
        assert "--schema" in capsys.readouterr().err
