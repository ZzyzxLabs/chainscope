"""Mixer timing correlation, scored against known ownership.

The technique is the one recorded in a real trace: thirteen deposits into one
pool, and for each the pool's *next* withdrawal was the matching one, twelve to
thirty-nine blocks later. Thirteen for thirteen.

What makes it worth measuring rather than trusting is that **the procedure is
identical whether or not it has any basis**. "The next withdrawal after mine" is
strong in a quiet pool and meaningless in a busy one, and nothing in the method
itself notices the difference. So the question this file answers is not "does it
work" --- it obviously did once --- but "how fast does it stop working, and does
the confidence follow it down".

Scored as precision over matched pairs. Recall matters less here for the usual
reason: an unmatched deposit costs coverage, while a wrong pairing puts a
stranger's address in a report next to real findings.
"""

from __future__ import annotations

import random

import pytest

from chainscope.analysis.mixer import (
    MAX_ANONYMITY_SET,
    MixerEvent,
    correlate_withdrawals,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM

SEED = 20260803


def build(
    *,
    operators: int,
    gap: int = 20,
    noise_per_deposit: int = 0,
    spacing: int = 200,
    rng: random.Random | None = None,
) -> tuple[list[MixerEvent], list[MixerEvent], dict[str, str]]:
    """A pool with ``operators`` deposit/withdraw pairs, plus unrelated traffic.

    ``noise_per_deposit`` is the lever that matters: unrelated withdrawals
    landing inside the same window, which is exactly what a busy pool looks
    like from outside.
    """
    r = rng or random.Random(SEED)
    deposits: list[MixerEvent] = []
    withdrawals: list[MixerEvent] = []
    truth: dict[str, str] = {}

    for i in range(operators):
        base = 1000 + i * spacing
        d = MixerEvent(tx=f"d{i:03d}", block=base, address=f"0xdepositor{i:03d}")
        w = MixerEvent(tx=f"w{i:03d}", block=base + gap, address=f"0xrecipient{i:03d}")
        deposits.append(d)
        withdrawals.append(w)
        truth[d.tx] = w.tx

        for n in range(noise_per_deposit):
            # Unrelated withdrawals inside the same window. Deliberately placed
            # *before* the true one sometimes, since a noise withdrawal that is
            # always later would never actually compete.
            offset = r.randint(1, max(2, gap * 2))
            withdrawals.append(
                MixerEvent(
                    tx=f"n{i:03d}_{n}",
                    block=base + offset,
                    address=f"0xstranger{i:03d}_{n}",
                )
            )
    return deposits, withdrawals, truth


def precision(result, truth: dict[str, str]) -> float:
    if not result.matches:
        return 1.0
    right = sum(1 for m in result.matches if truth.get(m.deposit.tx) == m.withdrawal.tx)
    return right / len(result.matches)


class TestTheRecordedCase:
    """Thirteen deposits, a quiet pool, gaps of twelve to thirty-nine blocks."""

    def test_it_recovers_every_pair(self):
        deposits, withdrawals, truth = build(operators=13)
        result = correlate_withdrawals(deposits, withdrawals)
        assert len(result.matches) == 13
        assert precision(result, truth) == 1.0

    def test_the_real_gaps_are_within_the_window(self):
        """Twelve to thirty-nine blocks, from the recorded trace."""
        r = random.Random(SEED)
        deposits, withdrawals, truth = [], [], {}
        for i, gap in enumerate([16, 24, 39, 19, 22, 12, 32, 14, 14, 24, 22, 28, 19]):
            base = 1000 + i * 500 + r.randint(0, 50)
            deposits.append(MixerEvent(tx=f"d{i}", block=base, address=f"0xd{i}"))
            withdrawals.append(MixerEvent(tx=f"w{i}", block=base + gap, address=f"0xr{i}"))
            truth[f"d{i}"] = f"w{i}"
        result = correlate_withdrawals(deposits, withdrawals)
        assert len(result.matches) == 13
        assert precision(result, truth) == 1.0

    def test_a_clean_match_is_medium_and_never_more(self):
        """The recorded case was as clean as this gets and it is still an
        inference about timing, not a break of the mixer."""
        deposits, withdrawals, _ = build(operators=3)
        for match in correlate_withdrawals(deposits, withdrawals).matches:
            assert match.confidence is Confidence.MEDIUM
            assert match.attribution(ETHEREUM).confidence <= Confidence.MEDIUM


class TestPrecisionAgainstPoolTraffic:
    """The number the anonymity set exists for."""

    @pytest.mark.parametrize("noise", [0, 1, 2, 4])
    def test_precision_falls_as_the_pool_gets_busier(self, noise):
        deposits, withdrawals, truth = build(operators=25, noise_per_deposit=noise)
        result = correlate_withdrawals(deposits, withdrawals)
        p = precision(result, truth)
        if noise == 0:
            assert p == 1.0
        else:
            # The point is not a particular number but that it is no longer 1
            # and that the claim weakens with it.
            assert p < 1.0

    def test_a_busy_pool_is_refused_rather_than_guessed(self):
        """Ten competitors is not a weak finding; it is a wrong one with a
        plausible shape."""
        deposits, withdrawals, _ = build(operators=15, noise_per_deposit=10)
        result = correlate_withdrawals(deposits, withdrawals)
        assert result.ambiguous
        assert len(result.matches) < len(deposits)

    def test_the_refusal_records_how_much_company_there_was(self):
        deposits, withdrawals, _ = build(operators=5, noise_per_deposit=10)
        result = correlate_withdrawals(deposits, withdrawals)
        assert all(n > MAX_ANONYMITY_SET for n in result.ambiguous.values())

    def test_confidence_tracks_the_anonymity_set(self):
        deposits, withdrawals, _ = build(operators=20, noise_per_deposit=3)
        for match in correlate_withdrawals(deposits, withdrawals).matches:
            if match.anonymity_set <= 1:
                assert match.confidence is Confidence.MEDIUM
            elif match.anonymity_set <= 3:
                assert match.confidence is Confidence.LOW
            else:
                assert match.confidence is Confidence.SPECULATIVE

    def test_a_wider_window_lowers_confidence_rather_than_finding_more(self):
        """Widening does not find more careful operators. It finds more
        competitors, and the honest response is weaker claims."""
        deposits, withdrawals, _ = build(operators=20, noise_per_deposit=2, spacing=60)
        narrow = correlate_withdrawals(deposits, withdrawals, window_blocks=30)
        wide = correlate_withdrawals(deposits, withdrawals, window_blocks=400)
        narrow_sets = [m.anonymity_set for m in narrow.matches]
        wide_sets = [m.anonymity_set for m in wide.matches]
        assert sum(wide_sets) / max(1, len(wide_sets)) > sum(narrow_sets) / max(
            1, len(narrow_sets)
        )


class TestWhatItRefusesToDo:
    def test_a_withdrawal_is_claimed_once(self):
        """Without this, one withdrawal near several deposits is assigned to
        all of them and a contested guess looks like a cluster of matches."""
        deposits = [
            MixerEvent(tx="d0", block=100, address="0xa"),
            MixerEvent(tx="d1", block=101, address="0xb"),
            MixerEvent(tx="d2", block=102, address="0xc"),
        ]
        withdrawals = [MixerEvent(tx="w0", block=110, address="0xr")]
        result = correlate_withdrawals(deposits, withdrawals)
        assert len(result.matches) == 1
        assert len(result.unmatched) == 2

    def test_a_withdrawal_before_the_deposit_is_not_a_candidate(self):
        deposits = [MixerEvent(tx="d0", block=200, address="0xa")]
        withdrawals = [MixerEvent(tx="w0", block=199, address="0xr")]
        assert not correlate_withdrawals(deposits, withdrawals).matches

    def test_same_block_ordering_is_respected(self):
        """A withdrawal earlier in the same block cannot be the answer to a
        deposit later in it."""
        deposits = [MixerEvent(tx="d0", block=200, index=5, address="0xa")]
        withdrawals = [MixerEvent(tx="w0", block=200, index=2, address="0xr")]
        assert not correlate_withdrawals(deposits, withdrawals).matches

    def test_a_deposit_outside_the_window_is_reported_not_dropped(self):
        """Nine matches from thirteen deposits reads as nine matches unless
        the other four are visible."""
        deposits = [MixerEvent(tx="d0", block=100, address="0xa")]
        withdrawals = [MixerEvent(tx="w0", block=100_000, address="0xr")]
        result = correlate_withdrawals(deposits, withdrawals)
        assert not result.matches
        assert [e.tx for e in result.unmatched] == ["d0"]

    def test_the_summary_accounts_for_every_deposit(self):
        deposits, withdrawals, _ = build(operators=13)
        result = correlate_withdrawals(deposits, withdrawals)
        assert "13 of 13" in result.summary()

    def test_an_invalid_window_is_refused(self):
        with pytest.raises(ValueError, match="window_blocks"):
            correlate_withdrawals([], [], window_blocks=0)


class TestTheClaimItself:
    def test_it_says_the_mixer_is_not_broken(self):
        deposits, withdrawals, _ = build(operators=1)
        claim = correlate_withdrawals(deposits, withdrawals).matches[0].attribution(ETHEREUM)
        assert "cryptography is not broken" in claim.rationale

    def test_it_names_the_operators_who_would_not_appear(self):
        """A depositor who waited is invisible to this, and a reader drawing
        conclusions about who used a mixer needs to know that."""
        deposits, withdrawals, _ = build(operators=1)
        claim = correlate_withdrawals(deposits, withdrawals).matches[0].attribution(ETHEREUM)
        assert "waited" in claim.rationale

    def test_a_contested_match_says_how_many_others_fit(self):
        deposits, withdrawals, _ = build(operators=6, noise_per_deposit=2)
        contested = [
            m
            for m in correlate_withdrawals(deposits, withdrawals).matches
            if m.anonymity_set > 1
        ]
        assert contested
        assert "equally consistent" in contested[0].summary()

    def test_the_claim_carries_a_source(self):
        deposits, withdrawals, _ = build(operators=1)
        assert correlate_withdrawals(deposits, withdrawals).matches[0].attribution().source


class TestTheShapeOfTheCollapse:
    """Why MAX_ANONYMITY_SET is five and not twenty.

    The intuition is that `n` competitors leave roughly `1/n` precision --- pick
    one of n+1 equally likely candidates. That would make a set of twenty
    tolerable at 5%.

    It is wrong, and wrong in the dangerous direction. A competitor does not win
    by being drawn at random; it wins by landing *anywhere earlier* than the
    true withdrawal. Precision is therefore the chance that none of them did,
    which falls geometrically. Measured against 400 deposits, the difference at
    four competitors is 7.5% versus the 20% the naive model predicts --- so a
    threshold chosen from intuition would have admitted claims that are wrong
    nine times in ten.
    """

    @pytest.mark.parametrize("competitors", [1, 2, 3, 4])
    def test_precision_follows_the_geometric_model(self, competitors):
        deposits, withdrawals, truth = build(operators=400, noise_per_deposit=competitors)
        result = correlate_withdrawals(deposits, withdrawals, max_anonymity_set=99)
        measured = precision(result, truth)
        geometric = 0.5**competitors
        uniform = 1 / (competitors + 1)
        assert abs(measured - geometric) < 0.06, (
            f"{competitors} competitors: measured {measured:.1%}, "
            f"geometric predicts {geometric:.1%}"
        )
        if competitors >= 2:
            # And is clearly *not* the uniform model, which is the one a
            # threshold would have been picked from.
            assert measured < uniform - 0.04

    def test_the_default_threshold_sits_where_precision_is_still_meaningful(self):
        """At the limit itself, a claim is SPECULATIVE and labelled so."""
        deposits, withdrawals, _ = build(operators=40, noise_per_deposit=MAX_ANONYMITY_SET - 1)
        for match in correlate_withdrawals(deposits, withdrawals).matches:
            assert match.anonymity_set <= MAX_ANONYMITY_SET
            if match.anonymity_set > 3:
                assert match.confidence is Confidence.SPECULATIVE
