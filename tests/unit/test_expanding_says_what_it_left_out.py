"""Expanding a node must report what it did not bring back.

Modelled on the "Advanced Analyze" dialog the commercial tools offer --- one
hop from a chosen address, filtered by direction, asset, value and time --- but
with the part those dialogs leave implicit made explicit.

A filter narrows what is fetched, so it also narrows what will ever be seen. An
asset filter that matches nothing, a time window that misses the transfer, and
an address that genuinely never moved money all end in the same small graph.
Only the last is a finding, so the response counts the exclusions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from chainscope.server.local import _sift, _stamp


class Edge:
    """Minimal stand-in with the fields `_sift` reads."""

    def __init__(self, *, asset=None, symbol="", total_raw=100, first=None, last=None):
        self.asset, self.symbol, self.total_raw = asset, symbol, total_raw
        self.first_seen, self.last_seen = first, last


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_an_asset_filter_matches_the_symbol_a_person_picked() -> None:
    """`asset` is the contract; the dropdown offers symbols.

    Comparing against the contract alone excluded everything whenever somebody
    chose "WETH" --- indistinguishable from an address that never held any.
    """
    edges = [Edge(asset="0xC02aaa...", symbol="WETH"), Edge(asset="0xdAC17...", symbol="USDT")]
    kept, dropped = _sift(edges, asset="WETH", since=None, until=None, min_raw="")
    assert len(kept) == 1 and dropped == 1


def test_an_asset_filter_still_matches_the_contract() -> None:
    """The contract is the real identity and must keep working."""
    edges = [Edge(asset="0xC02aaa", symbol="WETH")]
    kept, _ = _sift(edges, asset="0xc02AAA", since=None, until=None, min_raw="")
    assert len(kept) == 1, "contract matching is case-insensitive"


def test_exclusions_are_counted_not_silently_dropped() -> None:
    """Zero results with a reason is a different claim from zero results."""
    edges = [Edge(symbol="CASH") for _ in range(7)]
    kept, dropped = _sift(edges, asset="WETH", since=None, until=None, min_raw="")
    assert kept == [] and dropped == 7


def test_an_edge_spanning_the_window_is_inside_it() -> None:
    """An edge aggregates transfers; it is in the window if its span reaches."""
    edge = Edge(first=_when("2022-01-01"), last=_when("2022-06-01"))
    since = int(_when("2022-03-01").timestamp())
    kept, dropped = _sift([edge], asset="", since=since, until=None, min_raw="")
    assert len(kept) == 1 and dropped == 0, (
        "a flow that began before the window and ran into it belongs in it"
    )


def test_an_undated_edge_survives_a_time_filter() -> None:
    """No timestamp means the provider gave none, not that it fell outside."""
    kept, dropped = _sift(
        [Edge(first=None, last=None)], asset="", since=1_700_000_000, until=None, min_raw=""
    )
    assert len(kept) == 1 and dropped == 0


def test_a_value_floor_drops_dust_and_counts_it() -> None:
    edges = [Edge(total_raw=1), Edge(total_raw=10**18)]
    kept, dropped = _sift(edges, asset="", since=None, until=None, min_raw="1000")
    assert len(kept) == 1 and dropped == 1


def test_timestamps_convert_rather_than_raising() -> None:
    """`EdgeSummary` carries datetimes; the query carries unix seconds."""
    assert _stamp(None) is None
    assert _stamp(1_700_000_000) == 1_700_000_000
    assert _stamp(_when("2022-01-01")) == int(_when("2022-01-01").timestamp())


def test_the_page_no_longer_claims_it_never_fetches() -> None:
    """The docstring described the page as read-only; expanding fetches."""
    from pathlib import Path

    source = Path("src/chainscope/server/webapp.py").read_text()
    assert "Nothing here fetches from a chain" not in source
    assert "follow the money from here" in source.lower()
    # And the promise that reading alone stays free of network cost survives.
    assert "Reading never fetches" in source
