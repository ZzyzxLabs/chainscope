"""The needs note must keep its evidence line visible.

Its whole value is the distinction between what was watched going wrong and
what was reasoned from that. A version where every item reads the same is a
wishlist, and this project's argument is precisely about not presenting guesses
as findings --- a requirements document that does it would be the sharpest
possible contradiction.

So: both markers present, the caveat about the sample intact, and the open
items still labelled as inferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[2] / "docs" / "needs.md"


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text(encoding="utf-8")


class TestTheEvidenceLineIsVisible:
    def test_it_marks_observed_items(self, text):
        assert "**Observed.**" in text

    def test_it_marks_inferred_items(self, text):
        assert "*Inferred:*" in text or "*inferred*" in text.lower()

    def test_it_states_the_rule_up_front(self, text):
        head = text.split("---", 1)[0]
        assert "where the evidence came" in head

    def test_speculation_does_not_outnumber_evidence(self, text):
        """Not a style check. A note whose observed section is thin is a
        wishlist wearing a citation format."""
        assert text.count("**Observed.**") >= 4


class TestItAdmitsWhatItIsNot:
    def test_it_says_no_survey_was_conducted(self, text):
        assert "not a survey" in text.lower()

    def test_it_names_the_sample_bias(self, text):
        """One team, CTF-shaped work with known answers. Saying so is what
        keeps the note from reading as market research."""
        assert "bias" in text
        assert "CTF" in text

    def test_the_unhit_needs_are_labelled_as_inferred(self, text):
        section = text.split("Needs nobody has hit yet", 1)[1]
        assert "inferred" in section.lower()


class TestItCoversWhatTheSessionFound:
    @pytest.mark.parametrize(
        "evidence",
        [
            "eth_getLogs",  # silently short enumeration
            "hex digit",  # wrong block returned
            "OFAC SDN list",  # forgeable provenance
            "denominated in ETH",  # wrong native symbol
            "two rows in, one row out",  # lost transfer
        ],
    )
    def test_each_measured_failure_is_recorded(self, evidence, text):
        assert evidence in text

    def test_the_visualization_gap_is_named_not_glossed(self, text):
        assert "MetaSleuth" in text
        for missing in ("time axis", "Path highlight", "Click to expand"):
            assert missing in text
