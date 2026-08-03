"""Every caller consults the same sources, and none of them fails silently.

Two things converge here and both fail by looking normal.

**The CLI and the web server disagreed.** The server discovered every source
whose data was present; `chainscope label` required each to be named with a
flag and knew nothing about the three added later. So one install, one case
directory, and the same address resolved to a name in the browser and to
`unlabelled` in the terminal. An inconsistency like that is worse than a
missing feature --- a missing feature is visible, and this one tells two stories
about the same evidence.

**A source that is absent answers in the words of a clean result.** "No
attribution" and "nothing was consulted" are the same sentence on screen and
opposite facts, which is why `ready()` gates inclusion and why the commands say
so when nothing is present.

None of this path had a test. It was verified by hand with `curl`, and its
failure mode is the page quietly going back to showing `unlabelled` --- which is
indistinguishable from an honest answer. In a tool whose whole argument is
"never be silently wrong", that was the worst place to leave a hole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chainscope.attribution.build import available_sources, resolver_for
from chainscope.core.chainid import ETHEREUM

REAL_USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


@pytest.fixture
def labels(tmp_path: Path) -> Path:
    """A label directory holding one dataset of each shape."""
    root = tmp_path / "labels"
    root.mkdir()
    (root / "darklist.json").write_text(
        json.dumps([{"address": "0x" + "a" * 40, "comment": "phishing", "date": None}])
    )
    contracts = root / "contracts" / "contracts" / "1"
    contracts.mkdir(parents=True)
    (contracts / f"{REAL_USDT}.json").write_text(
        json.dumps({"project": "tether", "name": "Tether_USD", "source": "dune"})
    )
    return root


class TestDiscovery:
    def test_only_what_is_present_is_offered(self, labels: Path) -> None:
        names = {s.name for s in available_sources(labels)}
        assert names == {"darklist", "contracts_list"}

    def test_an_empty_directory_offers_nothing(self, tmp_path: Path) -> None:
        # And that is a legitimate answer, not an error. What must not happen is
        # the caller reporting it as "nothing known".
        assert available_sources(tmp_path) == []

    def test_sanctions_would_come_first(self, labels: Path) -> None:
        """Order is by what a claim is worth.

        The resolver merges without discarding, but something is primary, and a
        published designation must outrank a community list's guess. Ordering
        lives in one place because three call sites would eventually order it
        three ways.
        """
        # The shape `ofac` expects --- a list here raised, which is the fixture
        # being wrong rather than the source.
        (labels / "ofac.json").write_text(json.dumps({"entries": []}))
        assert available_sources(labels)[0].name == "ofac-sdn"

    def test_a_users_own_file_outranks_every_list(self, labels: Path) -> None:
        (labels / "local.json").write_text(json.dumps({"entries": []}))
        order = [s.name for s in available_sources(labels)]
        assert order.index("local") < order.index("contracts_list")


class TestEveryCallerUsesIt:
    """The inconsistency that prompted this file."""

    def test_the_cli_label_command_discovers(self) -> None:
        import inspect

        from chainscope.cli.commands import label

        assert "available_sources" in inspect.getsource(label.run)

    def test_investigate_passes_a_resolver(self) -> None:
        """`Context.resolver` was optional and this command passed None, so the
        one command that says "start here" ran every analyzer blind ---
        consolidation would find a hub and report it as unlabelled while the
        label sat in a file on disk."""
        import inspect

        from chainscope.cli.commands import investigate

        source = inspect.getsource(investigate.run)
        assert "resolver=resolver" in source

    def test_the_server_discovers(self) -> None:
        import inspect

        from chainscope.server import local

        assert "resolver_for" in inspect.getsource(local._Handlers._from_sources)

    def test_the_server_builds_it_once(self) -> None:
        """Constructing it per node rebuilt all six sources for every address,
        including a quarter-million-file index: 8.7 seconds for a twenty-node
        graph, against 0.51 cached."""
        import inspect

        assert "_resolver" in inspect.getsource(
            __import__("chainscope.server.local", fromlist=["local"])._Handlers._from_sources
        )


class TestAbsenceIsNotAnAnswer:
    def test_a_resolver_over_nothing_has_no_sources(self, tmp_path: Path) -> None:
        assert resolver_for(tmp_path).sources == []

    def test_the_labels_command_exits_non_zero_when_nothing_is_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Non-zero because a script that fetches then resolves needs to know the
        # fetch did not happen, and because every lookup afterwards will read as
        # "no attribution" while meaning "nothing was consulted".
        import argparse

        from chainscope.cli.commands import labels as labels_cmd
        from chainscope.render.terminal import TerminalRenderer

        args = argparse.Namespace(action="status", which=None, dir=tmp_path)
        code = labels_cmd.run(args, TerminalRenderer(colour=False))
        assert code == 1
        assert "not the same as an address being unknown" in capsys.readouterr().out

    def test_it_reports_what_is_present(
        self, labels: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import argparse

        from chainscope.cli.commands import labels as labels_cmd
        from chainscope.render.terminal import TerminalRenderer

        args = argparse.Namespace(action="status", which=None, dir=labels)
        assert labels_cmd.run(args, TerminalRenderer(colour=False)) == 0
        out = capsys.readouterr().out
        assert "[present] contracts_list" in out
        # The terms are printed, not buried in a docstring: two of these
        # datasets may not be redistributed and one is a repackaging.
        assert "NO LICENCE DECLARED" in out


class TestItActuallyResolves:
    def test_a_contract_is_named_through_the_shared_path(self, labels: Path) -> None:
        found = resolver_for(labels).resolve(REAL_USDT, ETHEREUM)
        assert found.entity is not None
        assert "tether" in found.entity.label

    def test_the_claim_carries_the_registry_own_source(self, labels: Path) -> None:
        # The field that makes this dump worth more than the others: it records
        # where its name came from.
        found = resolver_for(labels).resolve(REAL_USDT, ETHEREUM)
        assert "dune" in found.entity.primary.source  # type: ignore[union-attr]

    def test_an_unknown_address_resolves_to_nothing(self, labels: Path) -> None:
        found = resolver_for(labels).resolve("0x" + "7" * 40, ETHEREUM)
        assert found.entity is None
