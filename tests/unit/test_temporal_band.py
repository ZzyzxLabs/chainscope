"""A band that excludes nothing is not a finding.

Found by running the cold-start path --- fresh directory, no API key, first
real analysis --- and reading what it printed. It said:

    operating hours consistent with UTC-14 to UTC+6

Twenty hours wide, covering nearly every inhabited longitude, presented with a
bullet point beside it. Technically honest and a lie of emphasis: a reader
takes in "operating hours consistent with" and not the width that follows.
"""

from __future__ import annotations

import pytest

from chainscope.analysis.temporal import (
    MAX_USEFUL_BAND,
    ActivityProfile,
    temporal_attribution,
)


def profile(*, quiet, samples=200, concentration=0.5):
    p = ActivityProfile(address="0xa", samples=samples)
    p.concentration = concentration
    p.quiet_window_utc = quiet
    p.by_hour = tuple(1 for _ in range(24))
    p.peak_hour_utc = 12
    return p


class TestAWideBandIsRefused:
    def test_a_twenty_hour_quiet_window_reports_no_offset(self):
        """The recorded case: a window so wide the band spans most of the
        clock."""
        assert profile(quiet=(20, 19)).offset_range is None

    def test_and_makes_no_claim(self):
        """Quoting the point estimate alone would be the over-precision this
        module exists to avoid, and it is what a reader would carry away."""
        assert temporal_attribution(profile(quiet=(20, 19))) is None

    def test_a_narrow_window_still_reports(self):
        p = profile(quiet=(1, 8))
        assert p.offset_range is not None
        assert temporal_attribution(p) is not None

    def test_the_threshold_is_what_the_module_documents(self):
        assert MAX_USEFUL_BAND == 6

    @pytest.mark.parametrize("quiet", [(0, 23), (12, 11), (6, 5)])
    def test_windows_covering_the_clock_are_all_refused(self, quiet):
        assert profile(quiet=quiet).offset_range is None


class TestTheObservationSurvivesTheRefusal:
    def test_the_activity_is_still_described(self):
        """The measurement stands; only the inference is withheld."""
        summary = profile(quiet=(20, 19)).summary()
        assert "200 actions" in summary
        assert "peak at 12:00" in summary

    def test_it_says_why_no_offset_is_given(self):
        summary = profile(quiet=(20, 19)).summary()
        assert "too wide to place anyone" in summary
        assert "not inferable" in summary

    def test_it_does_not_print_a_degenerate_range(self):
        """ "UTC+0 to UTC+0" read as a precise answer produced by a calculation
        that had already given up."""
        assert "UTC+0 to UTC+0" not in profile(quiet=(20, 19)).summary()
