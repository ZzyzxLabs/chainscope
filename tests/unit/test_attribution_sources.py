"""Sources, and the resolver that reconciles them.

The load-bearing test in this file is
``test_a_failed_sanctions_source_does_not_read_as_clean``. Everything else is
plumbing by comparison: an empty screening result caused by a missing data file
is indistinguishable from a genuine all-clear unless something makes the
difference visible.
"""

import json

import pytest

from chainscope.attribution.base import SourceError
from chainscope.attribution.resolver import Resolver
from chainscope.attribution.sources.etherscan_dump import ExplorerDumpSource
from chainscope.attribution.sources.local import LocalSource
from chainscope.attribution.sources.ofac import OfacSource
from chainscope.core.attribution import Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM

TORNADO = "0xa160cdab225685da1d56aa342ad8841c3b53f291"
BINANCE = "0x28c6c06298d514db089934071355e5743bf21d60"
UNKNOWN = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def sdn(tmp_path):
    p = tmp_path / "ofac.json"
    p.write_text(
        json.dumps(
            {
                "fetched": "2026-08-01T00:00:00Z",
                "addresses": {TORNADO: {"label": "OFAC SDN (ETH)", "chain": "eip155:1"}},
            }
        )
    )
    return OfacSource(p)


@pytest.fixture
def nametags(tmp_path):
    p = tmp_path / "tags.json"
    p.write_text(
        json.dumps(
            {
                "_note": "file-level comment, must be skipped",
                BINANCE: {"label": "Binance 14", "tags": ["binance", "exchange"]},
                TORNADO: {"label": "Tornado.Cash: 100 ETH", "tags": ["tornado-cash"]},
            }
        )
    )
    return ExplorerDumpSource(p)


class TestOfac:
    def test_hit_is_certain_and_sanctioned(self, sdn):
        (claim,) = sdn.lookup(TORNADO)
        assert claim.category is Category.SANCTIONED
        assert claim.confidence is Confidence.CERTAIN
        assert claim.method is Method.LIST

    def test_source_string_records_the_snapshot(self, sdn):
        """So a claim can be traced to which extract produced it."""
        (claim,) = sdn.lookup(TORNADO)
        assert claim.source == "ofac-sdn@2026-08-01"

    def test_rationale_points_at_the_authoritative_list(self, sdn):
        (claim,) = sdn.lookup(TORNADO)
        assert "verify against the official publication" in claim.rationale

    def test_miss_is_empty(self, sdn):
        assert sdn.lookup(UNKNOWN) == []

    def test_lookup_is_case_insensitive(self, sdn):
        assert sdn.lookup(TORNADO.upper())

    def test_missing_file_raises_rather_than_returning_empty(self, tmp_path):
        s = OfacSource(tmp_path / "absent.json")
        assert not s.ready()
        with pytest.raises(SourceError, match="do not read an empty result"):
            s.lookup(TORNADO)


class TestExplorerDump:
    def test_label_becomes_a_high_confidence_claim(self, nametags):
        (claim,) = nametags.lookup(BINANCE)
        assert claim.label == "Binance 14"
        assert claim.confidence is Confidence.HIGH
        assert claim.method is Method.LABEL

    def test_cannot_assert_certain_even_if_asked(self, nametags):
        """A third-party label is strong, but it is not the chain speaking."""
        claim = nametags.emit(
            address=BINANCE,
            chain=ETHEREUM,
            label="x",
            category=Category.CEX,
            confidence=Confidence.CERTAIN,
            method=Method.LABEL,
        )
        assert claim.confidence is Confidence.HIGH
        assert "capped from CERTAIN" in claim.rationale

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Binance 14", Category.CEX),
            ("Tornado.Cash: 100 ETH", Category.MIXER),
            ("Uniswap V3: Router", Category.DEX),
            ("Wormhole: Token Bridge", Category.BRIDGE),
            ("Fake_Phishing1234", Category.ILLICIT),
            ("Some Random Project", Category.SERVICE),
        ],
    )
    def test_category_inference(self, tmp_path, label, expected):
        p = tmp_path / "t.json"
        p.write_text(json.dumps({UNKNOWN: {"label": label}}))
        (claim,) = ExplorerDumpSource(p).lookup(UNKNOWN)
        assert claim.category is expected

    def test_underscore_keys_are_notes_not_addresses(self, nametags):
        assert nametags.count == 2


class TestLocal:
    def test_round_trip(self, tmp_path):
        s = LocalSource(tmp_path / "mine.json")
        s.add(
            UNKNOWN,
            "Acme deposit cluster",
            category=Category.CEX,
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            rationale="17 single-use addresses consolidate here",
        )
        (claim,) = s.lookup(UNKNOWN)
        assert claim.label == "Acme deposit cluster"
        assert claim.confidence is Confidence.MEDIUM

    def test_weak_claim_needs_a_rationale_at_write_time(self, tmp_path):
        s = LocalSource(tmp_path / "mine.json")
        with pytest.raises(ValueError, match="what made you think this"):
            s.add(UNKNOWN, "a hunch", confidence=Confidence.LOW)

    def test_analyst_notes_cannot_outrank_published_labels(self, tmp_path):
        """MEDIUM ceiling by default: an opinion is not a publication."""
        s = LocalSource(tmp_path / "mine.json")
        s.add(UNKNOWN, "definitely Binance", confidence=Confidence.CERTAIN)
        (claim,) = s.lookup(UNKNOWN)
        assert claim.confidence is Confidence.MEDIUM

    def test_missing_file_is_not_ready(self, tmp_path):
        assert not LocalSource(tmp_path / "nope.json").ready()

    def test_malformed_json_is_reported_clearly(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        with pytest.raises(SourceError, match="not valid JSON"):
            LocalSource(p).lookup(UNKNOWN)


class TestResolver:
    def test_sanctions_win_over_a_friendly_label(self, sdn, nametags):
        r = Resolver().add(nametags).add(sdn)
        res = r.resolve(TORNADO)
        assert res.category is Category.SANCTIONED
        assert res.is_sanctioned is True
        assert len(res.entity.all_claims) == 2  # the mixer label is kept

    def test_unknown_address_resolves_to_nothing_found(self, sdn, nametags):
        res = Resolver().add(sdn).add(nametags).resolve(UNKNOWN)
        assert not res.found
        assert res.is_sanctioned is False
        assert res.reliable

    def test_a_failed_sanctions_source_does_not_read_as_clean(self, tmp_path, nametags):
        """The reason this module exists.

        With the sanctions list unreadable, "is this sanctioned?" must answer
        'unknown', not 'no'. Anything else turns a broken deployment into a
        confident all-clear.
        """
        broken = OfacSource(tmp_path / "missing.json")
        res = Resolver().add(broken).add(nametags).resolve(BINANCE)

        assert res.is_sanctioned is None  # not False
        assert not res.reliable
        assert res.failed
        assert "incomplete" in res.label or res.found

    def test_screen_keeps_addresses_it_could_not_check(self, tmp_path):
        """A screening function that drops unknowns produces a short,
        confident, incomplete list --- worse than no screening at all."""
        broken = OfacSource(tmp_path / "missing.json")
        flagged = Resolver().add(broken).screen([UNKNOWN, BINANCE])
        assert set(flagged) == {UNKNOWN, BINANCE}

    def test_one_broken_source_does_not_break_the_lookup(self, sdn, tmp_path):
        broken = LocalSource(tmp_path / "gone.json")
        res = Resolver().add(sdn).add(broken).resolve(TORNADO)
        assert res.is_sanctioned is True  # sdn still answered
        assert res.failed  # but the gap is recorded

    def test_terminal_detection(self, nametags):
        r = Resolver().add(nametags)
        assert r.terminal(BINANCE)  # exchange
        assert not r.terminal(UNKNOWN)

    def test_warnings_explain_weak_confidence(self, tmp_path):
        s = LocalSource(tmp_path / "mine.json")
        s.add(
            UNKNOWN,
            "possible cluster",
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            rationale="fan-in",
        )
        warnings = Resolver().add(s).resolve(UNKNOWN).warnings()
        assert any("treat as a lead, not a finding" in w for w in warnings)

    def test_offline_sources_are_queried_first(self, sdn, nametags):
        class Networked(LocalSource):
            offline = False

        r = Resolver().add(Networked(sdn.path)).add(nametags)
        assert r.sources[0].offline

    def test_results_are_cached(self, sdn):
        r = Resolver().add(sdn)
        assert r.resolve(TORNADO) is r.resolve(TORNADO)

    def test_bulk_resolution_matches_singles(self, sdn, nametags):
        r = Resolver().add(sdn).add(nametags)
        bulk = r.resolve_many([TORNADO, BINANCE, UNKNOWN])
        assert bulk[TORNADO].is_sanctioned is True
        assert bulk[BINANCE].label == "Binance 14"
        assert not bulk[UNKNOWN].found

    def test_citations_are_available_for_a_methodology_note(self, sdn, nametags):
        cites = Resolver().add(sdn).add(nametags).citations()
        assert any("OFAC" in c and "public domain" in c for c in cites)
