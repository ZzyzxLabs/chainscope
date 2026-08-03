"""Absent chain and invalid chain are different answers.

A claim with no chain applies everywhere --- that is how sanctions lists are
published, and it is correct. An *unrecognised* chain used to produce the same
`None`, so `--chain bsc`, the obvious thing to type, filed a claim against every
chain rather than against BSC. A typo did the same, and neither said anything.

Wrong-chain is bad. All-chains is worse: it looks answered, and it contaminates
every lookup that address takes part in. The graph layer works to keep a BSC
label off the Ethereum address sharing its hex; a claim scoped to nothing
defeats that from the other end.
"""

from __future__ import annotations

import json

import pytest

from chainscope.attribution.ingest import _to_chain, plan_import
from chainscope.cli.commands.tag import _chain
from chainscope.core.chainid import BSC, ETHEREUM

#: Full-length fixture addresses. They were three characters until the
#: importer started checking the address column, which is the sort of
#: shortcut that makes a test pass for the wrong reason.
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40


class TestTagChainArgument:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("bsc", BSC),
            ("bnb", BSC),
            ("ethereum", ETHEREUM),
            ("eth", ETHEREUM),
            ("1", ETHEREUM),
        ],
    )
    def test_an_alias_scopes_the_claim(self, raw, expected):
        assert _chain(raw) == expected

    def test_caip2_still_works(self):
        assert _chain("eip155:56") == BSC

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_means_every_chain(self, raw):
        """The one case where None is the right answer."""
        assert _chain(raw) is None

    @pytest.mark.parametrize("raw", ["etherium", "bsk", "not-a-chain", "eip155"])
    def test_an_unrecognised_chain_is_refused(self, raw):
        with pytest.raises(ValueError):
            _chain(raw)


class TestImportChainColumn:
    @pytest.mark.parametrize(("raw", "expected"), [("bsc", BSC), ("ethereum", ETHEREUM)])
    def test_a_name_scopes_the_row(self, raw, expected):
        assert _to_chain(raw) == expected

    def test_an_empty_cell_means_every_chain(self):
        assert _to_chain("") is None

    def test_a_typo_raises_rather_than_widening(self):
        with pytest.raises(ValueError):
            _to_chain("etherium")


class TestABadRowIsRejectedNotWidened:
    def test_the_typo_row_is_reported_and_the_others_import(self, tmp_path):
        path = tmp_path / "labels.json"
        path.write_text(
            json.dumps(
                [
                    {"address": A, "label": "Good", "chain": "bsc", "category": "cex"},
                    {"address": B, "label": "Typo", "chain": "etherium", "category": "cex"},
                    {
                        "address": C,
                        "label": "Global",
                        "chain": "",
                        "category": "sanctioned",
                    },
                ]
            )
        )
        plan = plan_import(path, source="test")

        assert len(plan.attributions) == 2
        assert len(plan.errors) == 1
        assert plan.errors[0].row == 2
        assert "etherium" in plan.errors[0].reason

        by_label = {a.label: a.chain for a in plan.attributions}
        assert by_label["Good"] == BSC
        # Still None, and deliberately: an empty cell is a claim about the
        # address wherever it appears.
        assert by_label["Global"] is None

    def test_one_bad_row_does_not_take_the_file_down(self, tmp_path):
        """A thirty-thousand-row file with one typo should import the rest."""
        path = tmp_path / "labels.json"
        rows = [
            {"address": f"0x{i:040x}", "label": f"L{i}", "chain": "eth", "category": "cex"}
            for i in range(20)
        ]
        rows.append({"address": D, "label": "X", "chain": "nope", "category": "cex"})
        path.write_text(json.dumps(rows))
        plan = plan_import(path, source="test")
        assert len(plan.attributions) == 20
        assert len(plan.errors) == 1
