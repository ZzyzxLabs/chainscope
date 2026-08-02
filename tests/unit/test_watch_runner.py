"""Running the rules that were only ever definitions.

`chainscope.watch` held its predicates as pure functions, tested that way, and
nothing ever ran them. Half of forensic work is being told when something moves
rather than looking back at what already did, so a rule engine with no runner
was the wrong half.

The tests below are mostly about the state file, because that is where a
monitor lies to you: a watch that has not run since block N is not a watch that
found nothing.
"""

from __future__ import annotations

import json

import pytest

from chainscope.cli.main import main
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.store.sqlite import SqliteStore

ETH = 10**18
WATCHED = "0xwatched"


@pytest.fixture
def case(tmp_path, monkeypatch):
    (tmp_path / ".chainscope").mkdir()
    store = SqliteStore(tmp_path / ".chainscope/store.db")
    store.put_transfers(
        [
            Transfer(
                chain=ETHEREUM,
                tx=TxRef(ETHEREUM, f"0x{i:064x}"),
                sender=Address(ETHEREUM, WATCHED, WATCHED),
                recipient=Address(ETHEREUM, f"0xdest{i}", f"0xdest{i}"),
                amount=Amount(v * ETH, 18, "ETH"),
                kind=TransferKind.NATIVE,
                block=100 + i,
                index=0,
            )
            for i, v in enumerate([5, 500, 20])
        ],
        source="t",
    )
    store.close()
    (tmp_path / "rules.json").write_text(
        json.dumps(
            [
                {
                    "name": "large-outflow",
                    "subject": WATCHED,
                    "when": {"kind": "amount_over", "raw": str(100 * ETH)},
                }
            ]
        )
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestItRuns:
    def test_a_rule_fires(self, case, capsys):
        assert main(["watch", "rules.json"]) == 0
        assert "large-outflow" in capsys.readouterr().out

    def test_nothing_firing_exits_non_zero(self, case, capsys):
        """So a shell loop can tell quiet from broken."""
        main(["watch", "rules.json"])
        capsys.readouterr()
        assert main(["watch", "rules.json"]) == 1

    def test_json_output_carries_the_transaction(self, case, capsys):
        main(["watch", "rules.json", "-F", "json"])
        events = json.loads(capsys.readouterr().out)
        assert events[0]["watch"] == "large-outflow"
        assert events[0]["tx"]


class TestTheStateFileIsWhereAMonitorLies:
    def test_a_second_run_does_not_repeat(self, case, capsys):
        main(["watch", "rules.json"])
        capsys.readouterr()
        main(["watch", "rules.json"])
        assert "large-outflow" not in capsys.readouterr().out

    def test_the_position_is_recorded(self, case):
        main(["watch", "rules.json"])
        state = json.loads((case / ".chainscope/watch-state.json").read_text())
        assert state["large-outflow"] == 102

    def test_dry_run_does_not_advance_it(self, case, capsys):
        """So somebody can look without consuming the events."""
        main(["watch", "rules.json", "--dry-run"])
        capsys.readouterr()
        assert not (case / ".chainscope/watch-state.json").exists()
        assert main(["watch", "rules.json"]) == 0

    def test_since_overrides_the_saved_position(self, case, capsys):
        main(["watch", "rules.json"])
        capsys.readouterr()
        main(["watch", "rules.json", "--since", "0"])
        assert "large-outflow" in capsys.readouterr().out

    def test_a_corrupt_state_file_is_refused_not_reset(self, case, capsys):
        """Resetting silently would re-scan from zero or skip a range, and
        neither is visible in the output."""
        (case / ".chainscope/watch-state.json").write_text("{not json")
        # A clean error and a non-zero exit, not a traceback: the CLI turns it
        # into a message that says what to do.
        assert main(["watch", "rules.json"]) != 0
        assert "delete it to start over" in capsys.readouterr().err


class TestBadRulesStopTheRun:
    def test_an_unknown_predicate_is_an_error(self, case, capsys):
        """Skipping it would produce a monitor quietly watching less than it
        was told to --- and nobody looks at a monitor that is not complaining."""
        (case / "rules.json").write_text(
            json.dumps([{"name": "x", "subject": WATCHED, "when": {"kind": "vibes"}}])
        )
        assert main(["watch", "rules.json"]) == 2
        assert "unknown predicate" in capsys.readouterr().err

    def test_the_failing_rule_is_numbered(self, case, capsys):
        (case / "rules.json").write_text(
            json.dumps(
                [
                    {
                        "name": "ok",
                        "subject": WATCHED,
                        "when": {"kind": "amount_over", "raw": "1"},
                    },
                    {"name": "bad", "subject": WATCHED, "when": {"kind": "nope"}},
                ]
            )
        )
        main(["watch", "rules.json"])
        assert "watch 2" in capsys.readouterr().err

    def test_an_empty_file_is_an_error(self, case, capsys):
        (case / "rules.json").write_text("[]")
        assert main(["watch", "rules.json"]) == 2

    def test_a_missing_store_is_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rules.json").write_text("[]")
        assert main(["watch", "rules.json"]) == 1
        assert "no store" in capsys.readouterr().err
