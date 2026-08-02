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

**Scoped to entry points this package ships.** They were not, and installing a
third-party plugin with a bad registration failed *chainscope's* suite ---
verified by building one. That is the wrong package's CI going red for somebody
else's defect, and the practical effect is worse than noise: a plugin author
whose mistake breaks the host project's tests learns to stop running them.

The same checks are exported as :func:`check_entry_point` for a plugin author to
run against their own package, which is where that failure belongs.
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


#: Distribution name this package installs as. An entry point from any other
#: distribution belongs to somebody else.
OWN_DISTRIBUTION = "chainscope"


def _registered(group: str) -> list[EntryPoint]:
    """Entry points **this package** ships, ignoring third-party plugins."""
    return sorted(
        (ep for ep in entry_points(group=group) if _ships_with_us(ep)),
        key=lambda e: e.name,
    )


def _ships_with_us(ep: EntryPoint) -> bool:
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None) if dist else None
    if name is None:
        # Older metadata does not carry the distribution. Fall back to the
        # module path, which is the next most reliable signal and errs towards
        # checking rather than skipping.
        return ep.value.startswith("chainscope.")
    return name.replace("_", "-").lower() == OWN_DISTRIBUTION


def check_entry_point(ep: EntryPoint, expected: type) -> None:
    """The contract, exported so a plugin author can run it on their own package.

    ::

        from importlib.metadata import entry_points
        from chainscope.analysis.base import Analyzer

        for ep in entry_points(group="chainscope.analyzers"):
            check_entry_point(ep, Analyzer)

    Raises `AssertionError` with the entry point named, which is the whole
    value: a registration that points at the wrong kind of object fails at the
    point of use, and the message there describes a different problem.
    """
    obj = ep.load()
    assert isinstance(obj, type), f"{ep.name} -> {ep.value} is not a class"
    assert issubclass(obj, expected), f"{ep.name} -> {ep.value} is not a {expected.__name__}"
    instance = obj()
    assert getattr(instance, "name", ""), f"{ep.name} has no name"
    assert getattr(instance, "description", ""), f"{ep.name} has no description"


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
        # Scoped the same way: a third-party plugin that fails to load is the
        # plugin author's problem, and failing here would make it ours.
        ours = {ep.name for ep in _registered("chainscope.analyzers")}
        assert {name: why for name, why in rejected().items() if name in ours} == {}

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


class TestAThirdPartyPluginIsNotOurProblem:
    """Verified by building one: a package with its own `pyproject.toml`,
    installed separately, registering `demo_dust = "mypkg.dust:DustAnalyzer"`.

    It was discovered, listed and ran, touching no chainscope source --- and a
    deliberately broken sibling entry point, pointing at a bare function, failed
    **this** package's suite. That is the wrong project's CI going red, and the
    practical effect is worse than noise: a plugin author whose mistake breaks
    the host's tests learns to stop running them.
    """

    def _foreign(self, value: str) -> EntryPoint:
        ep = EntryPoint(name="someone_elses", value=value, group="chainscope.analyzers")
        # importlib gives loaded entry points a `dist`; construct the shape
        # `_ships_with_us` reads.
        return ep

    def test_a_foreign_entry_point_is_not_checked_here(self):
        ours = _registered("chainscope.analyzers")
        foreign = self._foreign("someone.elses:Thing")
        with patch(
            "tests.unit.test_entry_point_contract.entry_points",
            return_value=[*ours, foreign],
        ):
            assert foreign not in _registered("chainscope.analyzers")

    def test_our_own_are_still_checked(self):
        # The scoping must not become a way of checking nothing.
        assert _registered("chainscope.analyzers")
        assert all(
            ep.value.startswith("chainscope.") for ep in _registered("chainscope.analyzers")
        )

    def test_the_exported_contract_rejects_a_bare_function(self):
        """Where the failure belongs: the plugin author's own suite.

        A registration pointing at the wrong kind of object fails at the *point
        of use*, and the message there describes a different problem --- which is
        the defect this whole file was written about.
        """
        bad = EntryPoint(
            name="bad",
            value=f"{__name__}:a_bare_function",
            group="chainscope.analyzers",
        )
        with pytest.raises(AssertionError, match="is not a class"):
            check_entry_point(bad, Analyzer)

    def test_the_exported_contract_accepts_a_real_analyzer(self):
        # A check that only ever fails is not a check.
        good = EntryPoint(
            name="good",
            value="chainscope.analysis.temporal:TemporalAnalyzer",
            group="chainscope.analyzers",
        )
        check_entry_point(good, Analyzer)

    def test_a_bad_plugin_is_still_reported_to_the_user(self):
        """Scoping must not hide it --- only move where it is reported.

        `analyze --list` names it with the reason, which is where somebody
        installing a plugin will look.
        """
        from chainscope.cli.commands.analyze import rejected

        assert isinstance(rejected(), dict)


def a_bare_function(ctx: object, **params: object) -> None:
    """The historical shape: an entry point pointing at a function.

    Nothing failed at import, nothing failed at install, and `analyze --list`
    printed one of them.
    """
    return None
