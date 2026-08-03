"""The case dashboard.

The behaviour that decides whether this is worth having: it leads with what is
*unfinished*. A summary opening with "1,284 transfers" tells an investigator
they have been busy; the numbers that decide whether a conclusion survives
review are the unlabelled count, the frontier, and how many claims rest on
weak evidence.
"""

import pytest

from chainscope.cli.commands.dashboard import build_summary
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount
from chainscope.render.dashboard import CaseSummary, to_dashboard
from chainscope.store.sqlite import SqliteStore

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40

TEN_ETH = 10 * 10**18


def transfer(sender, recipient, raw, *, block, symbol="ETH", decimals=18):
    return Transfer(
        chain=ETHEREUM,
        tx=TxRef(ETHEREUM, f"0x{block:064x}"),
        sender=Address(ETHEREUM, sender, sender),
        recipient=Address(ETHEREUM, recipient, recipient),
        amount=Amount(raw, decimals, symbol),
        kind=TransferKind.NATIVE,
        block=block,
        index=0,
    )


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "case.db"
    s = SqliteStore(path)
    s.put_transfers(
        [
            transfer(A, B, TEN_ETH, block=1),
            transfer(A, B, TEN_ETH, block=2),
            transfer(B, C, 5_000_000, block=3, symbol="USDC", decimals=6),
        ],
        source="t",
    )
    s.put_attributions(
        [
            Attribution(
                label="Binance 14",
                category=Category.CEX,
                confidence=Confidence.HIGH,
                method=Method.LABEL,
                source="etherscan",
                address=A,
                chain=ETHEREUM,
            ),
            Attribution(
                label="possibly a mixer",
                category=Category.MIXER,
                confidence=Confidence.LOW,
                method=Method.HEURISTIC,
                source="chainscope heuristics",
                address=B,
                chain=ETHEREUM,
                rationale="equal-value outputs, no change address",
            ),
        ]
    )
    s.mark_expanded(A, ETHEREUM)
    s.close()
    return path


class TestCoverageLeads:
    def test_unlabelled_addresses_are_counted(self, store_path):
        summary = build_summary(store_path)
        assert summary.unlabelled == 1  # C

    def test_coverage_is_a_fraction_of_addresses(self, store_path):
        summary = build_summary(store_path)
        assert summary.addresses == 3
        assert summary.coverage == pytest.approx(2 / 3)

    def test_weak_claims_are_counted_separately(self, store_path):
        """Not a defect --- an honest case has plenty --- but a reviewer will
        ask, and burying the count invites treating every label as solid."""
        assert build_summary(store_path).low_confidence == 1

    def test_the_frontier_is_what_was_never_expanded(self, store_path):
        summary = build_summary(store_path)
        assert summary.frontier == 2  # only A was expanded

    def test_an_empty_store_does_not_divide_by_zero(self, tmp_path):
        s = SqliteStore(tmp_path / "empty.db")
        s.close()
        summary = build_summary(tmp_path / "empty.db")
        assert summary.coverage == 0.0
        assert to_dashboard(summary)


class TestAmounts:
    def test_totals_are_exact_and_per_asset(self, store_path):
        totals = {
            sym: raw for sym, raw, _, _, _, _ in build_summary(store_path).totals_by_asset
        }
        assert totals["ETH"] == str(TEN_ETH * 2)
        assert totals["USDC"] == "5000000"

    def test_totals_are_strings(self, store_path):
        """20 ETH exceeds what a JSON number holds exactly."""
        for _, raw, _, _, _, _ in build_summary(store_path).totals_by_asset:
            assert isinstance(raw, str)

    def test_totals_carry_their_own_decimals(self, store_path):
        """The summary table assumed eighteen, so 5 USDC read as 0.000000.

        The flows table beside it had been reading `decimals` from the same
        rows all along --- so the dashboard showed the same asset correctly in
        one panel and a trillion times too small in the other.
        """
        places = {sym: d for sym, _, _, d, _, _ in build_summary(store_path).totals_by_asset}
        assert places["USDC"] == 6
        assert places["ETH"] == 18

    def test_the_summary_table_shows_usdc_as_usdc(self, store_path):
        page = to_dashboard(build_summary(store_path))
        assert "0.000000" not in page
        assert "5" in page

    def test_flows_carry_their_own_decimals(self, store_path):
        """A six-decimal amount rendered at eighteen is a trillion times too
        small."""
        flows = {f["symbol"]: f for f in build_summary(store_path).top_flows}
        assert flows["USDC"]["decimals"] == 6
        assert flows["ETH"]["decimals"] == 18

    def test_flows_are_ranked_within_an_asset_not_across(self, store_path):
        """Raw integers are not comparable across denominations: a wei total
        outranks any six-decimal one purely by magnitude."""
        summary = build_summary(store_path)
        symbols = [f["symbol"] for f in summary.top_flows]
        assert set(symbols) == {"ETH", "USDC"}

    def test_rendering_never_touches_a_float(self, store_path):
        page = to_dashboard(build_summary(store_path))
        assert "parseFloat" not in page
        assert "20" in page  # 20 ETH, grouped and exact


class TestRendering:
    def test_the_page_is_self_contained(self, store_path):
        import re

        page = to_dashboard(build_summary(store_path))
        assert not re.search(r"<script[^>]+\bsrc\s*=", page, re.I)
        assert not re.search(r"<link[^>]+href\s*=\s*[\"']https?://", page, re.I)

    def test_unfinished_work_is_phrased_as_work(self, store_path):
        page = to_dashboard(build_summary(store_path))
        assert "no attribution" in page
        assert "seen but never expanded" in page
        assert "not evidence of anything" in page

    def test_a_single_source_case_is_called_out(self, tmp_path):
        """One dump behind every claim is one point of failure for the whole
        attribution layer."""
        path = tmp_path / "one.db"
        s = SqliteStore(path)
        s.put_transfers([transfer(A, B, TEN_ETH, block=1)], source="t")
        s.put_attributions(
            [
                Attribution(
                    label="x",
                    category=Category.CEX,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="only-source",
                    address=A,
                    chain=ETHEREUM,
                )
            ]
        )
        s.close()
        assert "one point of failure" in to_dashboard(build_summary(path))

    def test_a_hostile_label_cannot_inject_markup(self, tmp_path):
        """Labels arrive from imported files and from agents."""
        path = tmp_path / "x.db"
        s = SqliteStore(path)
        s.put_transfers([transfer(A, B, TEN_ETH, block=1)], source="t")
        s.put_attributions(
            [
                Attribution(
                    label="</script><img src=x onerror=alert(1)>",
                    category=Category.CEX,
                    confidence=Confidence.HIGH,
                    method=Method.LABEL,
                    source="</td><script>alert(2)</script>",
                    address=A,
                    chain=ETHEREUM,
                )
            ]
        )
        s.close()
        page = to_dashboard(build_summary(path))
        assert "<img src=x" not in page
        assert "<script>alert(2)</script>" not in page

    def test_the_title_is_escaped(self):
        page = to_dashboard(CaseSummary(title="Case <b>7</b> & co"))
        assert "&lt;b&gt;" in page

    def test_it_parses_on_python_310(self):
        """A backslash inside an f-string expression is a syntax error before
        3.12, and this package supports 3.10."""
        import ast
        import pathlib

        import chainscope.render.dashboard as module

        ast.parse(pathlib.Path(module.__file__).read_text())


class TestReadOnly:
    def test_the_store_is_opened_read_only(self, store_path):
        """A view that can write to what it describes is a view that can be
        blamed for it."""
        before = store_path.stat().st_mtime
        build_summary(store_path)
        assert store_path.stat().st_mtime == before
