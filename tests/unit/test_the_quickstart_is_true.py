"""Every command the quickstart tells a new user to run must exist.

A quickstart is the first thing somebody runs and the last thing anybody
maintains. This one claims specific commands, specific flags, and specific
output; each claim is checked against the code, so a renamed flag fails here
rather than in front of a new user on their first ten minutes.

It is not a substitute for running it --- it was run, in order, from an empty
directory --- but that was true on one day and this stays true.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
QUICKSTART = DOCS / "quickstart.md"
HANDOVER = DOCS / "handover.md"


def _commands(text: str) -> set[str]:
    """The `chainscope <verb>` invocations a document tells somebody to run."""
    return set(re.findall(r"^chainscope (\w[\w-]*)", text, re.M))


class TestTheCommandsExist:
    def test_the_quickstart_names_some(self) -> None:
        # Guards the parsing: a quickstart this test cannot read would make
        # every assertion below vacuous.
        assert len(_commands(QUICKSTART.read_text())) >= 5

    @pytest.mark.parametrize("page", [QUICKSTART, HANDOVER], ids=lambda p: p.stem)
    def test_every_command_is_registered(self, page: Path) -> None:
        from chainscope.cli.main import _COMMANDS

        named = _commands(page.read_text())
        missing = sorted(named - set(_COMMANDS))
        assert not missing, f"{page.name} tells the reader to run {missing}"


class TestTheFlagsExist:
    def _flags(self, command: str) -> set[str]:
        import argparse

        from chainscope.cli.main import _COMMANDS

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        _COMMANDS[command].add_parser(sub, command)
        found: set[str] = set()
        for action in sub.choices[command]._actions:  # type: ignore[attr-defined]
            found.update(action.option_strings)
        return found

    @pytest.mark.parametrize(
        ("command", "flag"),
        [
            ("serve", "--writable"),
            ("serve", "--analyst"),
            ("labels", "--dir"),
            ("tag", "--label"),
            ("tag", "--category"),
            ("lead", "--kind"),
            ("investigate", "--labels"),
        ],
    )
    def test_a_flag_the_quickstart_uses(self, command: str, flag: str) -> None:
        assert flag in self._flags(command)


class TestTheClaimsHold:
    def test_labels_exits_non_zero_when_nothing_is_present(self, tmp_path: Path) -> None:
        """The quickstart says so, and a script relies on it.

        Without it, "no attribution" and "nothing was consulted" are the same
        exit code as well as the same sentence.
        """
        import argparse

        from chainscope.cli.commands import labels
        from chainscope.render.terminal import TerminalRenderer

        args = argparse.Namespace(action="status", which=None, dir=tmp_path)
        assert labels.run(args, TerminalRenderer(colour=False)) == 1

    def test_serve_is_read_only_by_default(self) -> None:
        # The quickstart says writing is a decision rather than a default, and
        # somebody will paste the command without reading the sentence.
        import argparse

        from chainscope.cli.commands import serve

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        serve.add_parser(sub, "serve")
        assert sub.choices["serve"].parse_args([]).writable is False

    def test_serve_has_no_host_flag(self) -> None:
        """Deliberately absent, and the handover page explains why.

        The store holds attributions somebody will act on and the server has no
        authentication beyond a per-run token. Binding wider is a thing to do
        on purpose, behind a proxy, not by passing a flag to a convenience
        command.
        """
        import argparse

        from chainscope.cli.commands import serve

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        serve.add_parser(sub, "serve")
        flags = {o for a in sub.choices["serve"]._actions for o in a.option_strings}
        assert "--host" not in flags

    def test_the_handover_page_warns_about_env(self) -> None:
        # The one file that must never travel, and the one a directory copied
        # wholesale will include.
        assert ".env" in HANDOVER.read_text()

    def test_it_says_which_files_are_rebuildable(self) -> None:
        # The split is the whole design: `store.db` is derived and `case.db` is
        # somebody's work, and a handover that confuses them loses the work.
        text = HANDOVER.read_text()
        assert "store.db" in text and "case.db" in text
        assert "cannot be recomputed" in text
