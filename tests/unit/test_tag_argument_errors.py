"""A mistyped argument gets a sentence, not a traceback.

`chainscope tag` is the command a person types by hand more than any other ---
it is how a judgement gets into the store. Two of its arguments raised bare
exceptions on a typo:

* ``--confidence hgih`` raised ``KeyError('HGIH')``. `_tag_one` catches
  ``ValueError`` and would have printed something; ``KeyError`` is not one, so
  it escaped both call sites and printed a traceback.
* ``--chain bsx`` in the *import* path raised ``ValueError`` that nothing
  caught, because that function caught only ``ImportError_``.

Neither is an import that ran and failed, so neither is exit 1. They are the
caller getting an argument wrong, which is exit 2 --- the same code `_tag_one`
already returned for the same mistakes. A script that distinguishes "your
arguments are wrong" from "the file had bad rows" could not, before this.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from chainscope.cli.commands.tag import _confidence, _import_file
from chainscope.core.attribution import Confidence
from chainscope.render.terminal import TerminalRenderer


class TestConfidence:
    def test_the_names_work(self) -> None:
        assert _confidence("high") == Confidence.HIGH
        assert _confidence("  Medium ") == Confidence.MEDIUM

    def test_the_numeric_form_still_works(self) -> None:
        # It appears in scripts; removing it would be a different change.
        assert _confidence("3") == Confidence.HIGH
        assert _confidence("0") == Confidence.SPECULATIVE

    def test_a_typo_raises_something_the_callers_catch(self) -> None:
        with pytest.raises(ValueError):
            _confidence("hgih")

    def test_a_typo_is_not_a_keyerror(self) -> None:
        # The actual defect. `KeyError` is not a `ValueError`, so every
        # `except ValueError` guarding this argument was decoration.
        with pytest.raises(Exception) as caught:
            _confidence("hgih")
        assert not isinstance(caught.value, KeyError)

    def test_the_message_lists_what_would_have_worked(self) -> None:
        with pytest.raises(ValueError, match="speculative, low, medium, high, certain"):
            _confidence("hgih")

    def test_out_of_range_reads_as_out_of_range(self) -> None:
        # Not "9 is not a valid Confidence", which is the enum complaining
        # about itself rather than telling the user the range.
        with pytest.raises(ValueError, match="out of range"):
            _confidence("9")


def _args(tmp_path: Path, **kw: object) -> argparse.Namespace:
    fields: dict[str, object] = {
        "store": tmp_path / "store.db",
        "source": "a list",
        "chain": None,
        "confidence": "high",
        "method": None,
        "apply": False,
        "limit": 10,
    }
    fields.update(kw)
    return argparse.Namespace(**fields)


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "labels.csv"
    path.write_text(
        "address,label,category\n0x0000000000000000000000000000000000000001,Binance,cex\n"
    )
    return path


class TestTheImportPath:
    def test_a_good_file_imports(self, tmp_path: Path) -> None:
        code = _import_file(_args(tmp_path), TerminalRenderer(), _csv(tmp_path))
        assert code == 0

    def test_a_bad_chain_is_an_argument_error(self, tmp_path: Path) -> None:
        code = _import_file(_args(tmp_path, chain="bsx"), TerminalRenderer(), _csv(tmp_path))
        assert code == 2

    def test_a_bad_confidence_is_an_argument_error(self, tmp_path: Path) -> None:
        code = _import_file(
            _args(tmp_path, confidence="hgih"), TerminalRenderer(), _csv(tmp_path)
        )
        assert code == 2

    def test_it_is_not_confused_with_a_file_that_had_bad_rows(self, tmp_path: Path) -> None:
        # Exit 1 means the import ran and rejected rows. A script that retries
        # on 1 and stops on 2 needs these to differ.
        bad = tmp_path / "bad.csv"
        bad.write_text("address,label,category\nnot-an-address,X,cex\n")
        assert _import_file(_args(tmp_path), TerminalRenderer(), bad) == 1

    def test_nothing_is_written_when_the_arguments_are_wrong(self, tmp_path: Path) -> None:
        args = _args(tmp_path, chain="bsx", apply=True)
        assert _import_file(args, TerminalRenderer(), _csv(tmp_path)) == 2
        # And the store the failure opened was closed and left empty rather
        # than half-populated.
        from chainscope.store.sqlite import SqliteStore

        store = SqliteStore(args.store)
        try:
            assert store.attributions("0x0000000000000000000000000000000000000001") == []
        finally:
            store.close()


class TestTheAddressColumnIsChecked:
    """The importer checked the label and the category and not the address.

    ``not-an-address`` imported clean and the report said "1 label ready". The
    mistake that matters here is not a person mistyping one address --- it is a
    column mapping off by one, or a header row read as data, which puts a name
    or a date in this column for *every row in the file*. Those attributions
    then sit in the store keyed on a string nothing will ever match, and
    `ingest`'s own docstring says getting thirty thousand mislabelled addresses
    back out is much harder than not writing them.

    Both directions are pinned, because the weak half is a deliberate choice: a
    chainless list is usually a sanctions publication carrying Monero and Ripple
    addresses this package has no adapter for, and rejecting those would throw
    away the rows most worth having.
    """

    def _plan(self, tmp_path: Path, body: str, chain: object = None) -> object:
        from chainscope.attribution.ingest import plan_import
        from chainscope.core.attribution import Method

        path = tmp_path / "labels.csv"
        path.write_text("address,label,category\n" + body)
        return plan_import(
            path,
            source="a list",
            chain=chain,  # type: ignore[arg-type]
            default_confidence=Confidence.HIGH,
            default_method=Method.LIST,
        )

    def test_a_shifted_column_is_rejected(self, tmp_path: Path) -> None:
        plan = self._plan(tmp_path, "Acme Trading Ltd,X,cex\n")
        assert plan.attributions == []  # type: ignore[attr-defined]
        assert "does not look like an address" in plan.errors[0].reason  # type: ignore[attr-defined]

    def test_a_header_row_read_as_data_is_rejected(self, tmp_path: Path) -> None:
        plan = self._plan(tmp_path, "address,label,category\n")
        assert plan.attributions == []  # type: ignore[attr-defined]

    def test_the_message_points_at_the_column_mapping(self, tmp_path: Path) -> None:
        # Because that is what is actually wrong, and the reader who sees one
        # bad row is about to see thirty thousand.
        plan = self._plan(tmp_path, "Acme Trading Ltd,X,cex\n")
        assert "column mapping" in plan.errors[0].reason  # type: ignore[attr-defined]

    def test_an_ordinary_address_still_imports(self, tmp_path: Path) -> None:
        plan = self._plan(tmp_path, "0x" + "1" * 40 + ",Binance,cex\n")
        assert len(plan.attributions) == 1  # type: ignore[attr-defined]

    def test_a_chain_this_package_cannot_parse_is_still_accepted(self, tmp_path: Path) -> None:
        # A real Monero address from a chainless list. There is no adapter for
        # it, and refusing everything unrecognised would drop exactly the rows
        # a sanctions file exists to carry.
        monero = (
            "4AdUndXHHZ6cfufTMvppY6JwXNouMBzSkbLYfpAV5Usx3skxNgYeYTRJ5AmD5"
            "H3F6XCA3nTAoJHXt1SsdrpBXKdvBhSJEUw"
        )
        plan = self._plan(tmp_path, f"{monero},Mixer,mixer\n")
        assert len(plan.attributions) == 1  # type: ignore[attr-defined]

    def test_with_a_chain_the_adapter_decides(self, tmp_path: Path) -> None:
        from chainscope.core.chainid import ETHEREUM

        plan = self._plan(tmp_path, "not-an-address,X,cex\n", chain=ETHEREUM)
        assert plan.attributions == []  # type: ignore[attr-defined]
        assert "not a valid address on eip155:1" in plan.errors[0].reason  # type: ignore[attr-defined]

    def test_the_wrong_chain_for_a_real_address_is_caught(self, tmp_path: Path) -> None:
        # A Solana address imported under --chain eth. Well-formed, real, and
        # about a different address space entirely.
        from chainscope.core.chainid import ETHEREUM

        plan = self._plan(
            tmp_path, "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM,X,cex\n", chain=ETHEREUM
        )
        assert plan.attributions == []  # type: ignore[attr-defined]

    def test_it_says_how_to_import_a_mixed_file(self, tmp_path: Path) -> None:
        from chainscope.core.chainid import ETHEREUM

        plan = self._plan(
            tmp_path, "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM,X,cex\n", chain=ETHEREUM
        )
        assert "chain column" in plan.errors[0].reason  # type: ignore[attr-defined]
