"""Community scam reports, and what a hit from one is worth.

Most of the OSINT sources the well-known recommendation lists name cannot be
reached at all. Checked: CryptoScamDB returns 502, Chainabuse 401, Etherscan's
label export 403 to anything that is not a browser, and Blockscout's
`public_tags` is in the schema and empty in practice.

`MyEtherWallet/ethereum-lists` answers: keyless, MIT, and each entry carries a
free-text comment and a date.

What the tests below protect is the *calibration*. This is a community list, so
it asserts MEDIUM where `ofac` asserts CERTAIN; its 715 entries are a rounding
error against the number of scam addresses in existence, so absence means
nothing; and it does not un-report, so a hit is a statement about the past and
carries its date.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chainscope.attribution.base import SourceError
from chainscope.attribution.sources.darklist import DarklistSource
from chainscope.core.attribution import Category, Confidence
from chainscope.core.chainid import BITCOIN, ETHEREUM

LISTED = "0x09750ad360fdb7a2ee23669c4503c974d86d8694"
UNLISTED = "0x28c6c06298d514db089934071355e5743bf21d60"


@pytest.fixture
def source(tmp_path: Path) -> DarklistSource:
    path = tmp_path / "darklist.json"
    path.write_text(
        json.dumps(
            [
                {
                    "address": LISTED,
                    "comment": "XRP phishing website (ripple.com.pt) this wallet "
                    "collects funds from victims",
                    "date": "2018-01-16T00:00:00.000Z",
                },
                {"address": "0x" + "b" * 40, "comment": "", "date": None},
                {"address": LISTED, "comment": "a second, different report", "date": None},
            ]
        )
    )
    return DarklistSource(path)


class TestCalibration:
    def test_a_hit_is_medium_not_certain(self, source: DarklistSource) -> None:
        # A community report is somebody's account of being defrauded. Real
        # evidence, and not the published legal fact `ofac` carries.
        found = source.lookup(LISTED, ETHEREUM)
        assert found[0].confidence == Confidence.MEDIUM

    def test_it_is_categorised_as_a_scam(self, source: DarklistSource) -> None:
        assert source.lookup(LISTED, ETHEREUM)[0].category == Category.SCAM

    def test_the_comment_is_kept_verbatim(self, source: DarklistSource) -> None:
        # "XRP phishing website (ripple.com.pt)…" tells an investigator what
        # happened. "scam" does not.
        rationale = source.lookup(LISTED, ETHEREUM)[0].rationale
        assert "ripple.com.pt" in rationale

    def test_the_report_date_travels_with_it(self, source: DarklistSource) -> None:
        # The list does not un-report, so a hit is a statement about the past.
        found = source.lookup(LISTED, ETHEREUM)[0]
        assert "2018-01-16" in found.rationale
        assert found.observed_at is not None
        assert found.observed_at.year == 2018

    def test_a_missing_date_is_not_today(self, source: DarklistSource) -> None:
        # Reading an absent date as now would turn an old report into a current
        # one --- the most misleading thing this file could do with a hole.
        assert source.lookup("0x" + "b" * 40, ETHEREUM)[0].observed_at is None

    def test_the_source_string_names_the_snapshot(self, source: DarklistSource) -> None:
        # So a hit can be checked against the publisher later.
        assert "darklist" in source.lookup(LISTED, ETHEREUM)[0].source


class TestAbsenceMeansNothing:
    def test_an_unlisted_address_returns_nothing(self, source: DarklistSource) -> None:
        assert source.lookup(UNLISTED, ETHEREUM) == []

    def test_a_non_ethereum_chain_raises_rather_than_returning_empty(
        self, source: DarklistSource
    ) -> None:
        """An unqualified empty result from a source that was never going to
        answer reads as a screening pass."""
        with pytest.raises(SourceError, match="Ethereum only"):
            source.lookup("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", BITCOIN)

    def test_the_refusal_says_it_is_not_a_clean_bill(self, source: DarklistSource) -> None:
        with pytest.raises(SourceError, match="different from saying the address is clean"):
            source.lookup("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", BITCOIN)

    def test_a_missing_file_raises_rather_than_screening_clean(self, tmp_path: Path) -> None:
        # The failure this whole shape exists for: no data looks exactly like
        # no reports.
        absent = DarklistSource(tmp_path / "nothing.json")
        assert not absent.ready()
        with pytest.raises(SourceError, match="not the same as clean"):
            absent.lookup(LISTED, ETHEREUM)

    def test_a_file_of_the_wrong_shape_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({"not": "a list"}))
        with pytest.raises(SourceError, match="not a JSON array"):
            DarklistSource(path).lookup(LISTED, ETHEREUM)


class TestAddressMatching:
    def test_a_checksummed_spelling_matches(self, source: DarklistSource) -> None:
        assert source.lookup(LISTED.upper().replace("0X", "0x"), ETHEREUM)

    def test_base58_case_is_not_folded(self) -> None:
        # `1BvBMSEY` and `1bvbmsey` are different addresses, and one of them
        # does not exist. On a list whose purpose is accusing an address, both
        # directions of that error are unacceptable.
        from chainscope.attribution.sources.darklist import _key

        assert _key("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2") != _key(
            "1bvbmseystwetqtfn5au4m4gfg7xjanvn2"
        )

    def test_the_first_report_wins(self, source: DarklistSource) -> None:
        # The list holds the same address twice with different comments.
        # Merging them would produce a rationale nobody wrote.
        found = source.lookup(LISTED, ETHEREUM)
        assert len(found) == 1
        assert "ripple.com.pt" in found[0].rationale


class TestItIsReachable:
    def test_it_is_registered_as_a_source(self) -> None:
        from importlib.metadata import entry_points

        names = {e.name for e in entry_points(group="chainscope.attribution_sources")}
        assert "darklist" in names

    def test_it_is_documented(self) -> None:
        # The repo's own gate checks this too; asserted here so the reason is
        # visible next to the code rather than only in a shell script.
        page = (Path(__file__).resolve().parents[2] / "docs" / "data-sources.md").read_text()
        assert "`darklist`" in page
        assert "MIT" in page
