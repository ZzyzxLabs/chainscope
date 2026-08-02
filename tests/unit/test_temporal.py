"""Temporal behaviour analysis.

Written against synthetic actors whose timezone is known, because that is the
only way to find out whether the estimate is any good rather than merely
plausible. The headline result: the point estimate lands one to three hours
off, consistently, and the reported band contains the truth in every case
tested. That is why the band is what appears in the label.

The other half of the tests is refusal. Most addresses have no timezone --- a
contract, an exchange wallet, anything scripted --- and a module that always
produces an answer turns every address into a person with a location.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from chainscope.analysis.temporal import (
    MIN_SAMPLES,
    profile_activity,
    temporal_attribution,
)
from chainscope.analysis.temporal import _circular_stats as circular_stats
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

SUBJECT = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday


def transfers(times, *, sender=SUBJECT, recipient=OTHER):
    return [
        Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, f"0x{i:064x}"),
            sender=Address(ETHEREUM, sender, sender),
            recipient=Address(ETHEREUM, recipient, recipient),
            amount=Amount(10**18, 18, "ETH"),
            kind=TransferKind.NATIVE,
            timestamp=t,
            block=i,
            index=i,
        )
        for i, t in enumerate(times)
    ]


def human_at(offset: int, *, days: int = 60, per_day: int = 6, seed: int = 1):
    """Someone active 08:00--23:00 local, at a known UTC offset."""
    rng = random.Random(seed + offset)
    out = []
    for d in range(days):
        for _ in range(per_day):
            local_hour = rng.choice(range(8, 24))
            out.append(
                BASE
                + timedelta(
                    days=d, hours=(local_hour - offset) % 24, minutes=rng.randint(0, 59)
                )
            )
    return out


class TestCircularArithmetic:
    def test_midnight_crossing_breaks_the_arithmetic_mean(self):
        """An actor working 22:00-02:00 has an arithmetic mean of noon --- the
        one hour they are never active. This is not a rounding concern."""
        hours = [22, 23, 0, 1, 2] * 20
        arithmetic = sum(hours) / len(hours)
        circular, _ = circular_stats(hours)
        assert 8 < arithmetic < 12
        assert circular < 1 or circular > 23

    def test_concentration_is_low_for_round_the_clock_activity(self):
        _, concentration = circular_stats(list(range(24)) * 10)
        assert concentration < 0.05

    def test_concentration_is_high_for_a_narrow_window(self):
        _, concentration = circular_stats([9, 10, 11] * 30)
        assert concentration > 0.9

    def test_no_samples_does_not_divide_by_zero(self):
        assert circular_stats([]) == (0.0, 0.0)


class TestEstimateAgainstKnownTruth:
    @pytest.mark.parametrize("truth", [-8, -5, 0, 1, 3, 5, 8, 9])
    def test_the_band_contains_the_real_offset(self, truth):
        """The property that justifies reporting anything at all."""
        profile = profile_activity(transfers(human_at(truth)), SUBJECT)
        band = profile.offset_range
        assert band is not None, f"no estimate for UTC{truth:+d}"
        assert band[0] <= truth <= band[1]

    def test_the_point_estimate_is_close_but_not_exact(self):
        """Documents the systematic error the band exists to cover: the
        estimate assumes a 03:00 local sleep centre and real hours are not that
        tidy."""
        errors = [
            abs(profile_activity(transfers(human_at(t)), SUBJECT).likely_utc_offset - t)
            for t in (-8, 0, 3, 8)
        ]
        assert max(errors) <= 3
        assert any(e > 0 for e in errors), "an exact estimate would be suspicious"

    def test_the_label_quotes_a_band_not_a_point(self):
        """Whatever is in the label is what gets quoted, and a bare "UTC+6"
        would be repeated as though measured."""
        profile = profile_activity(transfers(human_at(8)), SUBJECT)
        claim = temporal_attribution(profile, ETHEREUM)
        assert claim is not None
        assert " to UTC" in claim.label

    def test_a_claim_never_exceeds_medium(self):
        """Timing narrows a hypothesis; it cannot confirm one."""
        profile = profile_activity(transfers(human_at(3, days=200)), SUBJECT)
        claim = temporal_attribution(profile, ETHEREUM)
        assert claim is not None
        assert claim.confidence <= Confidence.MEDIUM


class TestRefusal:
    def test_round_the_clock_activity_yields_no_offset(self):
        """Normal for contracts, exchange wallets, and anything run from more
        than one place. Reporting an offset would invent a person."""
        rng = random.Random(3)
        times = [BASE + timedelta(hours=rng.random() * 24 * 90) for _ in range(400)]
        profile = profile_activity(transfers(times), SUBJECT)
        assert not profile.has_diurnal_pattern
        assert profile.likely_utc_offset is None
        assert temporal_attribution(profile) is None

    def test_too_few_samples_yields_nothing(self):
        profile = profile_activity(transfers(human_at(5)[:12]), SUBJECT)
        assert profile.samples < MIN_SAMPLES
        assert not profile.has_diurnal_pattern
        assert temporal_attribution(profile) is None

    def test_no_timestamps_at_all_is_handled(self):
        rows = transfers([BASE])
        rows[0] = Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, "0x" + "0" * 64),
            sender=Address(ETHEREUM, SUBJECT, SUBJECT),
            recipient=Address(ETHEREUM, OTHER, OTHER),
            amount=Amount(1, 18, "ETH"),
            kind=TransferKind.NATIVE,
            timestamp=None,
        )
        profile = profile_activity(rows, SUBJECT)
        assert profile.samples == 0
        assert temporal_attribution(profile) is None

    def test_the_summary_explains_a_refusal(self):
        profile = profile_activity(transfers(human_at(5)[:12]), SUBJECT)
        assert "too few" in profile.summary()


class TestAutomation:
    def _scheduled(self, hours=4, n=300):
        return [BASE + timedelta(hours=hours * i) for i in range(n)]

    def test_evenly_spaced_activity_is_flagged(self):
        profile = profile_activity(transfers(self._scheduled()), SUBJECT)
        assert profile.interval_cv is not None
        assert profile.interval_cv < 0.01
        assert profile.looks_automated

    def test_an_automated_address_gets_no_timezone(self):
        profile = profile_activity(transfers(self._scheduled()), SUBJECT)
        claim = temporal_attribution(profile, ETHEREUM)
        assert claim is not None
        assert "scheduled" in claim.label
        assert claim.confidence is Confidence.LOW

    def test_a_human_is_not_flagged_as_automated(self):
        profile = profile_activity(transfers(human_at(2)), SUBJECT)
        assert not profile.looks_automated

    def test_weekend_ratio_is_per_day_not_per_total(self):
        """A five-to-two split would otherwise read as a weekend lull that is
        not there."""
        rng = random.Random(9)
        times = [
            BASE + timedelta(days=d, hours=rng.randint(0, 23))
            for d in range(70)
            for _ in range(5)
        ]
        profile = profile_activity(transfers(times), SUBJECT)
        assert 0.7 < profile.weekend_ratio < 1.4


class TestDirection:
    def test_only_the_address_own_actions_count(self):
        """Inbound transfers are somebody else's timing; folding them in
        measures the union of two schedules, which is nobody's."""
        mine = transfers(human_at(8), sender=SUBJECT)
        theirs = transfers(human_at(-5), sender=OTHER, recipient=SUBJECT)
        profile = profile_activity(mine + theirs, SUBJECT, direction="out")
        assert profile.samples == len(mine)

    def test_inbound_can_be_profiled_explicitly(self):
        theirs = transfers(human_at(-5), sender=OTHER, recipient=SUBJECT)
        profile = profile_activity(theirs, SUBJECT, direction="in")
        assert profile.samples == len(theirs)


class TestSerialisation:
    def test_the_profile_round_trips_to_a_dict(self):
        profile = profile_activity(transfers(human_at(3)), SUBJECT)
        data = profile.to_dict()
        assert len(data["by_hour_utc"]) == 24
        assert len(data["by_weekday"]) == 7
        assert sum(data["by_hour_utc"]) == profile.samples

    def test_the_rationale_carries_the_evidence(self):
        """A LOW or MEDIUM claim with no reasoning cannot even be constructed,
        and one with a vague reasoning is not much better."""
        profile = profile_activity(transfers(human_at(8)), SUBJECT)
        claim = temporal_attribution(profile, ETHEREUM)
        assert claim is not None
        assert "UTC" in claim.rationale
        assert str(profile.samples) in claim.rationale
