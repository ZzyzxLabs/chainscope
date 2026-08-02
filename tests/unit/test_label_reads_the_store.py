"""`chainscope label` must find what `chainscope tag` just wrote.

The obvious sequence --- record a label, then look it up --- answered "no
sources configured". The user's own label was in the store the whole time; the
command only consulted external sources. A tool that cannot find what it just
recorded reads as broken, and reasonably so.

Caught by running the workflow end to end rather than by a test, which is why
these exist now.
"""

from __future__ import annotations

import pytest

from chainscope.cli.main import main
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.store.sqlite import SqliteStore

ADDRESS = "0x" + "a" * 40


@pytest.fixture
def case(tmp_path, monkeypatch):
    (tmp_path / ".chainscope").mkdir()
    store = SqliteStore(tmp_path / ".chainscope/store.db")
    store.put_attributions(
        [
            Attribution(
                label="eXch hot wallet",
                category=Category.CEX,
                confidence=Confidence.MEDIUM,
                method=Method.LIST,
                source="team-labels",
                address=ADDRESS,
                chain=ETHEREUM,
                rationale="from case notes",
            )
        ]
    )
    store.close()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestItFindsWhatWasJustRecorded:
    def test_the_label_comes_back(self, case, capsys):
        assert main(["label", ADDRESS]) == 0
        assert "eXch hot wallet" in capsys.readouterr().out

    def test_the_confidence_and_source_come_with_it(self, case, capsys):
        main(["label", ADDRESS])
        out = capsys.readouterr().out
        assert "medium" in out
        assert "team-labels" in out

    def test_the_rationale_is_shown(self, case, capsys):
        main(["label", ADDRESS])
        assert "from case notes" in capsys.readouterr().out

    def test_it_says_where_the_claim_came_from(self, case, capsys):
        """So a store claim is never mistaken for a sanctions hit."""
        main(["label", ADDRESS])
        assert "case store" in capsys.readouterr().out


class TestChainScoping:
    def test_a_claim_from_another_chain_is_not_shown(self, case, capsys):
        """The same twenty bytes exist on every EVM chain, and a BSC label says
        nothing about the Ethereum address sharing its hex."""
        assert main(["label", ADDRESS, "--chain", "bsc"]) == 2
        assert "eXch hot wallet" not in capsys.readouterr().out

    def test_a_chain_agnostic_claim_shows_everywhere(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".chainscope").mkdir()
        store = SqliteStore(tmp_path / ".chainscope/store.db")
        store.put_attributions(
            [
                Attribution(
                    label="OFAC SDN",
                    category=Category.SANCTIONED,
                    confidence=Confidence.CERTAIN,
                    method=Method.LIST,
                    source="ofac",
                    address=ADDRESS,
                    chain=None,
                )
            ]
        )
        store.close()
        monkeypatch.chdir(tmp_path)
        assert main(["label", ADDRESS, "--chain", "bsc"]) == 0
        assert "OFAC SDN" in capsys.readouterr().out
        assert BSC  # the point is it is not filtered out


class TestAnEmptyAnswerIsNotAnAbsence:
    def test_an_unlabelled_address_says_what_it_means(self, case, capsys):
        assert main(["label", "0x" + "b" * 40]) == 2
        err = capsys.readouterr().out
        assert "not evidence it is unlabelled" in err

    def test_no_store_and_no_sources_says_that_instead(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["label", ADDRESS]) == 2
        assert "nothing to search" in capsys.readouterr().out

    def test_the_store_can_be_skipped(self, case, capsys):
        assert main(["label", ADDRESS, "--no-store"]) == 2
        assert "eXch hot wallet" not in capsys.readouterr().out
