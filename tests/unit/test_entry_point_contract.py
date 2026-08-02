"""Every registered entry point must satisfy the contract it claims.

Two of this package's own analyzer entry points once pointed at plain
functions. Nothing failed at import, nothing failed at install, and
``analyze --list`` happily printed one of them --- so the only symptom was
`chainscope analyze temporal` answering "needs constructor arguments (a data
source) ... use the Python API", which is a true sentence about a different
problem and sends the reader looking in the wrong place. The other was invisible
outright.

That is the shape of failure this project exists to refuse: a wrong answer
delivered confidently. Registration is a promise about an object's type, and a
promise nothing checks is decoration.

These tests read the *installed metadata*, not a hand-maintained list, so
registering a seventh analyzer wrongly fails here without anyone remembering to
update a test.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from unittest.mock import patch

import pytest

from chainscope.analysis.base import Analyzer
from chainscope.cli.commands.analyze import _discover, available, rejected

GROUPS = {
    "chainscope.analyzers": Analyzer,
}


def _registered(group: str) -> list[EntryPoint]:
    return sorted(entry_points(group=group), key=lambda e: e.name)


class TestThisPackagesOwnRegistrations:
    def test_there_are_analyzers_registered_at_all(self):
        """Guards the rest of this file: if discovery silently returned nothing,
        every other assertion here would pass vacuously."""
        assert _registered("chainscope.analyzers")

    @pytest.mark.parametrize("ep", _registered("chainscope.analyzers"), ids=lambda e: e.name)
    def test_every_analyzer_entry_point_is_an_analyzer_subclass(self, ep: EntryPoint):
        obj = ep.load()
        assert isinstance(obj, type), f"{ep.name} -> {ep.value} is not a class"
        assert issubclass(obj, Analyzer), f"{ep.name} -> {ep.value} is not an Analyzer"

    @pytest.mark.parametrize("ep", _registered("chainscope.analyzers"), ids=lambda e: e.name)
    def test_every_analyzer_constructs_with_no_arguments(self, ep: EntryPoint):
        """The CLI instantiates with ``cls()``. An analyzer that cannot be built
        that way is reachable only from Python, which is not what registering it
        advertises."""
        ep.load()()

    @pytest.mark.parametrize("ep", _registered("chainscope.analyzers"), ids=lambda e: e.name)
    def test_every_analyzer_describes_itself(self, ep: EntryPoint):
        """``--list`` is how anybody finds these. A blank line is a tool the
        user cannot tell apart from the others."""
        instance = ep.load()()
        assert instance.description.strip()
        assert instance.name != "unnamed"

    def test_nothing_this_package_registers_is_rejected(self):
        assert rejected() == {}

    def test_discovery_finds_the_documented_six(self):
        """A floor, not a fixed list --- plugins may add more. It fails if one
        of ours stops being discoverable, which is the regression that
        prompted this file."""
        found = set(available())
        assert {
            "co_spend_cluster",
            "common_funder",
            "consolidation",
            "cross_chain",
            "peel_chain",
            "temporal",
        } <= found


class TestDiscoveryReportsWhatItRejects:
    """Rejects are returned rather than dropped. A plugin that fails to import
    is where silence costs most: the user installed it, ``--list`` does not
    mention it, and nothing says why."""

    def _discover_with(self, *eps: EntryPoint):
        with patch("chainscope.cli.commands.analyze.entry_points", return_value=list(eps)):
            return _discover()

    def test_a_function_is_rejected_as_a_function(self):
        ok, broken = self._discover_with(
            EntryPoint(
                "bare", "chainscope.analysis.funding:cluster_by_funder", "chainscope.analyzers"
            )
        )
        assert ok == {}
        assert "is a function" in broken["bare"]
        assert "not an Analyzer" in broken["bare"]

    def test_an_unimportable_target_names_the_error(self):
        ok, broken = self._discover_with(
            EntryPoint("gone", "chainscope.nope:Thing", "chainscope.analyzers")
        )
        assert ok == {}
        assert "ModuleNotFoundError" in broken["gone"]

    def test_an_unrelated_class_is_rejected(self):
        ok, broken = self._discover_with(
            EntryPoint("wrong", "chainscope.core.result:Finding", "chainscope.analyzers")
        )
        assert ok == {}
        assert "not an Analyzer" in broken["wrong"]

    def test_one_broken_plugin_does_not_hide_the_working_ones(self):
        """The reason discovery catches rather than raises: a third-party
        plugin failing to import must not take the tool down with it."""
        ok, broken = self._discover_with(
            EntryPoint("gone", "chainscope.nope:Thing", "chainscope.analyzers"),
            EntryPoint(
                "good",
                "chainscope.analysis.funding:CommonFunderAnalyzer",
                "chainscope.analyzers",
            ),
        )
        assert list(ok) == ["good"]
        assert list(broken) == ["gone"]
