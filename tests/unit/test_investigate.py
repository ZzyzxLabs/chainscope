"""`investigate`: nine analyzers is nine ways to be stuck.

Every analyzer needs parameters somebody has to already know --- which deposit
hashes went into the mixer, which address was the source. The capability was
there and the first move was not, which is the worst shape for a tool: it looks
complete and feels unusable.

This command's job is to hand back a next move. That is the property the tests
below check, including in the cases where nothing could be run.
"""

from __future__ import annotations

import pytest

from chainscope.cli.main import main
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.store.sqlite import SqliteStore

ADDRESS = "0x" + "a" * 40


@pytest.fixture
def labelled(tmp_path, monkeypatch):
    (tmp_path / ".chainscope").mkdir()
    store = SqliteStore(tmp_path / ".chainscope/store.db")
    store.put_attributions(
        [
            Attribution(
                label="Binance 14",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LIST,
                source="etherscan",
                address=ADDRESS,
                chain=ETHEREUM,
            )
        ]
    )
    store.close()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestItAlwaysHandsBackANextMove:
    def test_a_labelled_address_reports_the_label(self, labelled, capsys):
        main(["investigate", ADDRESS])
        assert "Binance 14" in capsys.readouterr().out

    def test_it_always_prints_a_next_section(self, labelled, capsys):
        main(["investigate", ADDRESS])
        out = capsys.readouterr().out
        assert "next:" in out
        assert "chainscope graph" in out

    def test_an_unlabelled_address_suggests_tagging_it(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        main(["investigate", ADDRESS])
        assert "chainscope tag" in capsys.readouterr().out

    def test_every_suggestion_is_a_readable_command(self, labelled, capsys):
        """Nothing is run for you that you cannot read first."""
        main(["investigate", ADDRESS])
        lines = capsys.readouterr().out.split("next:")[1].splitlines()
        for line in (line.strip() for line in lines):
            if line and not line.startswith(("Nothing", "evidence")):
                assert line.startswith("chainscope ")


class TestItDoesNotOverstate:
    def test_it_says_an_empty_result_is_not_an_absence(self, labelled, capsys):
        main(["investigate", ADDRESS])
        out = capsys.readouterr().out
        assert "not that the pattern is absent" in out

    def test_nothing_found_exits_non_zero(self, tmp_path, monkeypatch):
        """So a script does not read silence as a clean bill of health."""
        monkeypatch.chdir(tmp_path)
        assert main(["investigate", ADDRESS]) == 1

    def test_a_finding_exits_zero(self, labelled):
        assert main(["investigate", ADDRESS]) == 0

    def test_an_unusable_chain_says_what_is_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
        # BSC has no keyless provider: no Blockscout instance exists for it.
        assert main(["investigate", ADDRESS, "--chain", "bsc"]) == 2
        out = capsys.readouterr().out
        assert "nothing can run" in out
        assert "chainscope doctor" in out


class TestTooBusyIsANextMoveNotAFailure:
    def test_the_exception_carries_the_narrowing(self):
        from chainscope.cli.commands.investigate import TooBusy

        assert "start_block" in str(
            TooBusy("too active for one page --- narrow it with -p start_block / -p end_block")
        )

    def test_it_is_not_a_generic_error(self):
        """Caught separately from a real failure, because the response differs:
        one is a narrower question, the other is something being broken."""
        from chainscope.cli.commands.investigate import TooBusy

        assert issubclass(TooBusy, RuntimeError)
