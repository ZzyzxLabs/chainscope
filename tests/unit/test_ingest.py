"""Bulk label import.

The behaviours worth pinning are the ones that decide whether somebody's first
ten minutes with this are productive: that it reads the file they already have,
that a bad row does not lose the good ones, and that the provenance rules bite
at import rather than three tools downstream.
"""

import json

import pytest

from chainscope.attribution.ingest import (
    ImportError_,
    ingest_file,
    parse_rows,
    plan_import,
)
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.store.sqlite import SqliteStore

A = "0x28C6c06298d514Db089934071355E5743bf21d60"
B = "0xA160cdAB225685dA1d56aa342Ad8841c3b53f291"


@pytest.fixture
def csv_file(tmp_path):
    def write(text: str, name: str = "labels.csv"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return write


class TestReadingWhatPeopleActuallyHave:
    def test_a_spreadsheet_export_with_its_own_column_names(self, csv_file):
        """ "wallet"/"name"/"type" is far more likely than a schema nobody read."""
        path = csv_file(f"wallet,name,type,certainty\n{A},Binance 14,exchange,high\n")
        plan = plan_import(path, source="team")
        assert len(plan.attributions) == 1
        assert plan.attributions[0].category is Category.CEX
        assert plan.attributions[0].confidence is Confidence.HIGH

    def test_a_byte_order_mark_does_not_hide_the_first_column(self, tmp_path):
        """Every spreadsheet export starts with one, and without utf-8-sig the
        first column name silently becomes '\\ufeffaddress'."""
        path = tmp_path / "bom.csv"
        path.write_bytes(b"\xef\xbb\xbfaddress,label\n" + f"{A},Binance\n".encode())
        assert len(plan_import(path, source="team").attributions) == 1

    def test_json_list(self, tmp_path):
        path = tmp_path / "l.json"
        path.write_text(json.dumps([{"address": A, "label": "Binance", "category": "cex"}]))
        assert len(plan_import(path, source="team").attributions) == 1

    def test_json_keyed_by_address(self, tmp_path):
        """The shape label files usually take."""
        path = tmp_path / "k.json"
        path.write_text(json.dumps({A: {"label": "Binance", "category": "cex"}}))
        got = plan_import(path, source="team").attributions
        assert got[0].address == A

    def test_json_mapping_address_to_a_bare_name(self, tmp_path):
        path = tmp_path / "flat.json"
        path.write_text(json.dumps({A: "Binance 14"}))
        assert plan_import(path, source="team").attributions[0].label == "Binance 14"

    def test_jsonl(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps({"address": A, "label": "Binance"})
            + "\n\n"
            + json.dumps({"address": B, "label": "Tornado", "category": "mixer"})
            + "\n"
        )
        assert len(plan_import(path, source="team").attributions) == 2

    def test_an_unknown_extension_says_what_is_supported(self, tmp_path):
        path = tmp_path / "labels.xlsx"
        path.write_text("nope")
        with pytest.raises(ImportError_, match=r"\.csv, \.json, \.jsonl"):
            plan_import(path, source="team")

    def test_a_missing_file_is_clear(self, tmp_path):
        with pytest.raises(ImportError_, match="no such file"):
            plan_import(tmp_path / "absent.csv", source="team")


class TestProvenanceIsEnforcedAtImport:
    def test_an_import_without_a_source_is_refused(self):
        with pytest.raises(ImportError_, match="needs a source"):
            parse_rows([{"address": A, "label": "x"}], source="  ")

    def test_low_confidence_without_a_rationale_is_rejected_by_row(self, csv_file):
        """The type system's rule, surfaced where it is actionable."""
        path = csv_file(f"address,label,confidence\n{A},Guess,low\n")
        plan = plan_import(path, source="team")
        assert plan.attributions == []
        assert "rationale" in plan.errors[0].reason

    def test_low_confidence_with_a_rationale_passes(self, csv_file):
        path = csv_file(f"address,label,confidence,reason\n{A},Guess,low,timing correlation\n")
        assert len(plan_import(path, source="team").attributions) == 1

    def test_the_source_reaches_every_attribution(self, csv_file):
        path = csv_file(f"address,label\n{A},Binance\n{B},Tornado\n")
        plan = plan_import(path, source="ofac-2026-08")
        assert {a.source for a in plan.attributions} == {"ofac-2026-08"}


class TestBadRowsDoNotLoseGoodOnes:
    def test_one_bad_row_among_many(self, csv_file):
        path = csv_file(f"address,label\n{A},Binance\n,Missing address\n{B},Tornado\n")
        plan = plan_import(path, source="team")
        assert len(plan.attributions) == 2
        assert len(plan.errors) == 1

    def test_errors_carry_the_row_number(self, csv_file):
        """ "invalid category" in a 30,000-line file is not actionable without it."""
        path = csv_file(f"address,label,category\n{A},Binance,cex\n{B},X,nonsense\n")
        plan = plan_import(path, source="team")
        assert plan.errors[0].row == 2
        assert "nonsense" in plan.errors[0].reason

    def test_an_unknown_category_lists_the_known_ones(self, csv_file):
        path = csv_file(f"address,label,category\n{A},X,teapot\n")
        assert "cex" in plan_import(path, source="team").errors[0].reason

    def test_an_unknown_confidence_explains_the_options(self, csv_file):
        path = csv_file(f"address,label,confidence\n{A},X,quite sure\n")
        assert "0-4" in plan_import(path, source="team").errors[0].reason

    def test_the_plan_is_not_ok_when_rows_failed(self, csv_file):
        path = csv_file(f"address,label\n{A},Binance\n,\n")
        assert not plan_import(path, source="team").ok


class TestDeduplicationAndConflicts:
    def test_duplicate_rows_within_a_file_collapse(self, csv_file):
        path = csv_file(f"address,label\n{A},Binance\n{A},Binance\n{A},binance\n")
        plan = plan_import(path, source="team")
        assert len(plan.attributions) == 1
        assert plan.duplicates == 2

    def test_two_different_labels_for_one_address_both_survive(self, csv_file):
        """Disagreement is often the interesting part, not a data-quality bug."""
        path = csv_file(f"address,label\n{A},Binance\n{A},Something else\n")
        assert len(plan_import(path, source="team").attributions) == 2

    def test_a_conflict_with_stored_data_is_reported(self, csv_file):
        path = csv_file(f"address,label,category\n{A},Suspicious mixer,mixer\n")
        existing = {
            A: [
                Attribution(
                    label="Binance 14",
                    category=Category.CEX,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="etherscan",
                    address=A,
                    chain=ETHEREUM,
                )
            ]
        }
        plan = plan_import(path, source="team", existing=existing)
        assert len(plan.conflicts) == 1
        assert "mixer" in str(plan.conflicts[0])

    def test_sanctions_do_not_count_as_disagreement(self, csv_file):
        """A sanctioned mixer is both. Counting that as a conflict would fire
        on nearly every sanctioned entity."""
        path = csv_file(f"address,label,category\n{B},Tornado Cash,sanctioned\n")
        existing = {
            B: [
                Attribution(
                    label="Tornado Cash",
                    category=Category.MIXER,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="etherscan",
                    address=B,
                    chain=ETHEREUM,
                )
            ]
        }
        assert plan_import(path, source="team", existing=existing).conflicts == []


class TestChains:
    def test_a_chain_column_is_read_per_row(self, csv_file):
        path = csv_file(f"address,label,chain\n{A},Binance,1\n{B},PancakeSwap,56\n")
        chains = [a.chain for a in plan_import(path, source="team").attributions]
        assert chains == [ETHEREUM, BSC]

    def test_caip2_is_accepted(self, csv_file):
        path = csv_file(f"address,label,chain\n{A},Binance,eip155:56\n")
        assert plan_import(path, source="team").attributions[0].chain == BSC

    def test_a_default_chain_applies_when_the_file_has_no_column(self, csv_file):
        path = csv_file(f"address,label\n{A},Binance\n")
        got = plan_import(path, source="team", chain=BSC).attributions[0]
        assert got.chain == BSC


class TestApplying:
    def test_a_dry_run_writes_nothing(self, csv_file, tmp_path):
        path = csv_file(f"address,label\n{A},Binance\n")
        store = SqliteStore(tmp_path / "s.db")
        try:
            ingest_file(path, store, source="team", apply=False)
            assert store.attributions(A) == []
        finally:
            store.close()

    def test_apply_writes(self, csv_file, tmp_path):
        path = csv_file(f"address,label\n{A},Binance\n")
        store = SqliteStore(tmp_path / "s.db")
        try:
            plan = ingest_file(path, store, source="team", apply=True)
            assert len(plan.attributions) == 1
            assert store.attributions(A)
        finally:
            store.close()

    def test_the_summary_breaks_down_by_category(self, csv_file):
        path = csv_file(f"address,label,category\n{A},Binance,cex\n{B},Tornado,sanctioned\n")
        summary = plan_import(path, source="team").summary()
        assert summary["by_category"] == {"cex": 1, "sanctioned": 1}
        assert summary["source"] == "team"
