"""Provenance is not optional.

These tests encode the module's central promise: you cannot express an
attribution without saying where it came from, and a weak claim cannot
impersonate a strong one.

Fixtures use publicly documented addresses --- the Ronin Bridge exploiter
(OFAC SDN, April 2022), Tornado Cash pools (OFAC SDN, August 2022), and a
Binance hot wallet with a published block-explorer nametag. Nothing here is
novel intelligence; it is all a matter of public record.
"""

from datetime import datetime, timezone

import pytest

from chainscope.core.attribution import (
    Attribution,
    Category,
    Confidence,
    Method,
    merge,
)
from chainscope.core.chainid import BITCOIN, ETHEREUM

# Ronin Bridge exploiter, added to the OFAC SDN list in April 2022.
RONIN_EXPLOITER = "0x098b716b8aaf21512996dc57eb0615e2383e2f96"
# Tornado Cash 100 ETH pool, added to the OFAC SDN list in August 2022.
TORNADO_100 = "0xa160cdab225685da1d56aa342ad8841c3b53f291"
# Binance hot wallet, public nametag "Binance 14".
BINANCE_14 = "0x28c6c06298d514db089934071355e5743bf21d60"
# An arbitrary bech32 address, used only to exercise low-confidence claims.
UNLABELLED_BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def labelled(**kw: object) -> Attribution:
    base = dict(
        address=BINANCE_14,
        chain=ETHEREUM,
        label="Binance 14",
        category=Category.CEX,
        confidence=Confidence.HIGH,
        method=Method.LABEL,
        source="etherscan-nametag@2026-08",
    )
    return Attribution(**{**base, **kw})  # type: ignore[arg-type]


class TestConstruction:
    def test_source_is_mandatory(self):
        with pytest.raises(ValueError, match="needs a source"):
            labelled(source="   ")

    def test_label_is_mandatory(self):
        with pytest.raises(ValueError, match="non-empty label"):
            labelled(label="")

    def test_weak_claims_must_justify_themselves(self):
        # The whole point: you may assert a guess, but you must say why.
        with pytest.raises(ValueError, match="requires a rationale"):
            Attribution(
                address=UNLABELLED_BTC,
                chain=BITCOIN,
                label="Some exchange",
                category=Category.CEX,
                confidence=Confidence.LOW,
                method=Method.INFERENCE,
                source="analyst",
            )

    def test_weak_claim_with_rationale_is_allowed(self):
        a = Attribution(
            address=UNLABELLED_BTC,
            chain=BITCOIN,
            label="Some exchange",
            category=Category.CEX,
            confidence=Confidence.LOW,
            method=Method.INFERENCE,
            source="analyst",
            rationale=(
                "Payouts settle at a consistent discount to spot across three "
                "independent swaps, and traced funds return to this wallet."
            ),
        )
        assert a.confidence is Confidence.LOW

    def test_strong_claims_need_no_rationale(self):
        assert labelled().rationale == ""


class TestConfidence:
    def test_ordering_supports_thresholds(self):
        assert Confidence.CERTAIN > Confidence.HIGH > Confidence.MEDIUM
        assert Confidence.MEDIUM > Confidence.LOW > Confidence.SPECULATIVE

    @pytest.mark.parametrize(
        ("level", "actionable"),
        [
            (Confidence.CERTAIN, True),
            (Confidence.HIGH, True),
            (Confidence.MEDIUM, False),
            (Confidence.LOW, False),
            (Confidence.SPECULATIVE, False),
        ],
    )
    def test_only_strong_claims_are_actionable(self, level, actionable):
        assert level.is_actionable is actionable

    def test_weak_claims_are_marked_in_display(self):
        weak = Attribution(
            address=UNLABELLED_BTC,
            chain=BITCOIN,
            label="Some exchange",
            category=Category.CEX,
            confidence=Confidence.LOW,
            method=Method.INFERENCE,
            source="analyst",
            rationale="fee-rate match",
        )
        assert weak.display.endswith("?")
        assert labelled().display == "Binance 14"


class TestCategory:
    def test_terminal_categories(self):
        assert Category.CEX.is_terminal
        assert Category.MIXER.is_terminal
        assert Category.BRIDGE.is_terminal
        assert Category.SANCTIONED.is_terminal

    def test_non_terminal_categories(self):
        assert not Category.SUSPECT.is_terminal
        assert not Category.TOKEN.is_terminal
        assert not Category.UNKNOWN.is_terminal


class TestMerge:
    def test_empty_yields_none(self):
        assert merge([]) is None

    def test_sanctions_take_the_primary_slot(self):
        """A friendly service label must never bury a sanctions hit."""
        friendly = Attribution(
            address=TORNADO_100,
            chain=ETHEREUM,
            label="Tornado.Cash: 100 ETH",
            category=Category.MIXER,
            confidence=Confidence.HIGH,
            method=Method.LABEL,
            source="etherscan-nametag",
        )
        sanctioned = Attribution(
            address=TORNADO_100,
            chain=ETHEREUM,
            label="OFAC SDN",
            category=Category.SANCTIONED,
            confidence=Confidence.CERTAIN,
            method=Method.LIST,
            source="ofac-sdn@2026-08-01",
        )
        e = merge([friendly, sanctioned])
        assert e.category is Category.SANCTIONED
        assert e.is_sanctioned
        assert len(e.all_claims) == 2  # nothing is discarded

    def test_sanctions_win_even_at_lower_confidence(self):
        strong = labelled(
            address=RONIN_EXPLOITER,
            confidence=Confidence.CERTAIN,
            category=Category.SUSPECT,
            label="Ronin Bridge Exploiter",
        )
        weak_sanction = Attribution(
            address=RONIN_EXPLOITER,
            chain=ETHEREUM,
            label="possible SDN match",
            category=Category.SANCTIONED,
            confidence=Confidence.LOW,
            method=Method.INFERENCE,
            source="analyst",
            rationale="address resembles a listed entity",
        )
        assert merge([strong, weak_sanction]).category is Category.SANCTIONED

    def test_higher_confidence_wins_otherwise(self):
        weak = Attribution(
            address=BINANCE_14,
            chain=ETHEREUM,
            label="unknown service",
            category=Category.SERVICE,
            confidence=Confidence.MEDIUM,
            method=Method.HEURISTIC,
            source="consolidation-analysis",
            rationale="many single-use deposit addresses funnel here",
        )
        assert merge([weak, labelled()]).label == "Binance 14"

    def test_recency_breaks_remaining_ties(self):
        old = labelled(label="Old Name", observed_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = labelled(label="New Name", observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert merge([old, new]).label == "New Name"

    def test_disagreement_is_surfaced_not_hidden(self):
        a = labelled(category=Category.CEX)
        b = labelled(category=Category.MIXER, source="other-source")
        e = merge([a, b])
        assert e.disputed
        assert "disagree" in str(e)

    def test_weak_claims_do_not_create_a_dispute(self):
        e = merge(
            [
                labelled(category=Category.CEX),
                Attribution(
                    address=BINANCE_14,
                    chain=ETHEREUM,
                    label="maybe a mixer",
                    category=Category.MIXER,
                    confidence=Confidence.LOW,
                    method=Method.INFERENCE,
                    source="analyst",
                    rationale="hunch",
                ),
            ]
        )
        assert not e.disputed

    def test_terminal_if_any_claim_is_terminal(self):
        e = merge(
            [
                labelled(
                    category=Category.SUSPECT,
                    confidence=Confidence.MEDIUM,
                    method=Method.HEURISTIC,
                    rationale="high deposit fan-in",
                ),
                labelled(category=Category.CEX, source="etherscan"),
            ]
        )
        assert e.is_terminal
