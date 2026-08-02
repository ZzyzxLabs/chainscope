"""The skill must describe the tool that exists, not the one that did.

A skill is instructions an agent follows instead of exploring. That makes a
stale one worse than none: an agent reading it will not know a capability
exists, and will confidently report that the tool cannot answer a question it
answers. This file caught exactly that --- five analyzers built in one session
and none of them mentioned.

So the check reads the *installed metadata* rather than a hand-kept list. A
tenth analyzer that nobody documents fails here without anyone remembering to
add it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chainscope.cli.commands.analyze import available
from chainscope.cli.main import _COMMANDS

SKILL = Path(__file__).resolve().parents[2] / "skills" / "chainscope" / "SKILL.md"


@pytest.fixture(scope="module")
def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_check_found_something() -> None:
    """Both lists below are parametrised over discovered sets.

    Empty, they collect zero cases and this file passes green while checking
    nothing --- the same vacuous pass this project has now written a guard
    against three times. It belongs next to every `parametrize` over a
    discovered set, not in the one file where it was noticed.
    """
    assert available(), "no analyzers discovered; the entry-point lookup is stale"
    assert _COMMANDS, "the CLI dispatch table is empty or was renamed"


class TestItCoversWhatIsInstalled:
    @pytest.mark.parametrize("name", sorted(available()))
    def test_every_registered_analyzer_is_mentioned(self, name, text):
        assert name in text, (
            f"{name} is installed and the skill does not mention it. An agent "
            f"reading this will report that chainscope cannot do it."
        )

    @pytest.mark.parametrize("command", sorted(_COMMANDS))
    def test_every_cli_command_is_mentioned(self, command, text):
        """Read from the dispatch table, not from a list kept by hand.

        The hand-kept version named seven commands and stayed green through
        five more being added --- which is the failure this whole file exists
        to prevent, in the file that exists to prevent it.
        """
        assert f"chainscope {command}" in text, (
            f"`{command}` is a command and the skill does not show it being run. "
            f"An agent reading this will report that chainscope cannot do it."
        )


class TestItCarriesTheQualifiers:
    """A capability listed without the number that bounds it is worse than one
    left out: it invites a confident answer where the technique does not work."""

    def test_probing_states_its_false_positive_rate(self, text):
        assert "38%" in text

    def test_mixer_states_how_precision_decays(self, text):
        assert "8.3%" in text

    def test_common_funder_states_the_cost_of_dropping_the_guard(self, text):
        assert "0.7%" in text

    def test_cross_chain_states_that_it_ranks_a_decoy(self, text):
        assert "decoy" in text

    def test_taint_separates_holding_from_touching(self, text):
        # Whitespace-normalised: the source is wrapped, and a test that breaks
        # on rewrapping teaches people to delete it.
        flat = " ".join(text.split())
        assert "passed through here" in flat
        assert "holds stolen value" in flat

    def test_it_says_an_empty_result_is_not_absence(self, text):
        assert "not evidence of absence" in text

    def test_it_says_a_truncated_result_is_not_complete(self, text):
        assert "truncated" in text


class TestTheFileIsUsable:
    def test_it_has_frontmatter_with_a_name_and_description(self, text):
        assert text.startswith("---\n")
        head = text.split("---", 2)[1]
        assert "name: chainscope" in head
        assert "description:" in head

    def test_the_description_says_when_to_use_it(self, text):
        """A description that only says what the tool *is* never matches a
        request."""
        head = text.split("---", 2)[1]
        assert "Use when" in head
