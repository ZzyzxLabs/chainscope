"""Temporal behaviour: when an address acts, and what that suggests.

The oldest OSINT technique applied to the newest data. When somebody transacts
says something about them that the transactions themselves do not: a consistent
eight-hour gap every day is a person sleeping, and where that gap sits in UTC
narrows down where they are. Attribution work on state-sponsored groups has
leaned on exactly this for years.

It is also the technique most easily overstated, so the constraints are built
in rather than left to the reader.

**Hours are circular.** 23:00 and 00:00 are adjacent, and an arithmetic mean
over hour numbers puts an actor who works 22:00--02:00 squarely at noon. The
centre of activity is computed as a circular mean --- sum the unit vectors,
take the angle --- which is the only version that is not wrong for exactly the
actors most worth examining.

**Most addresses have no timezone at all.** A contract, an exchange hot wallet,
or anything scripted runs around the clock. Reporting an offset for one of
those is inventing a person. So the profile reports *whether a diurnal pattern
exists* before it reports where it sits, and refuses the second when the first
is weak.

**Sample size decides confidence, and small samples are the norm.** Twelve
transactions can look strikingly regular by chance. The confidence returned
falls out of the sample size and the strength of the pattern, and below a floor
the answer is "not enough data" rather than a guess with a wide error bar ---
because a stated guess gets quoted and an error bar does not.

What this produces is a claim about *operating hours*, at
:class:`~chainscope.core.attribution.Confidence.LOW` or
:attr:`~chainscope.core.attribution.Confidence.MEDIUM`, with the histogram as
its rationale. It is not an identity, and the wording is chosen so that it
cannot be quoted as one.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ..chains import address_key
from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context, history_of

__all__ = [
    "MIN_SAMPLES",
    "ActivityProfile",
    "TemporalAnalyzer",
    "Timed",
    "profile_activity",
    "temporal_attribution",
]

#: Below this, no pattern claim is made at all. Twelve timestamps can look
#: strikingly regular by chance, and a stated guess gets quoted while its error
#: bar does not.
MIN_SAMPLES = 30

#: Concentration below which the diurnal pattern is treated as absent. Derived
#: from the mean resultant length of the circular distribution: 0 is uniform
#: (around the clock, so no timezone to report) and 1 is a single instant.
MIN_CONCENTRATION = 0.25

#: Widest half-band, in hours, still worth reporting.
#:
#: Six either side is a twelve-hour window --- half the clock --- and that is
#: already generous. Beyond it the claim excludes so little that stating it
#: misleads by emphasis: a reader takes in "operating hours consistent with"
#: and not the width that follows.
MAX_USEFUL_BAND = 6

#: Coefficient of variation below which inter-transaction gaps look scheduled
#: rather than human. Human activity is bursty; a cron job is not.
AUTOMATION_CV = 0.35


@dataclass
class ActivityProfile:
    """When an address acted, and what can honestly be said about it."""

    address: str
    samples: int
    by_hour: tuple[int, ...] = field(default_factory=lambda: (0,) * 24)
    by_weekday: tuple[int, ...] = field(default_factory=lambda: (0,) * 7)

    concentration: float = 0.0
    """Mean resultant length of the hour distribution, 0--1. How much the
    activity clusters in the day rather than spreading around the clock."""

    peak_hour_utc: int | None = None
    quiet_window_utc: tuple[int, int] | None = None
    """Longest run of low-activity hours --- the candidate sleep window."""

    weekend_ratio: float = 0.0
    """Weekend activity per day divided by weekday activity per day. Near 1.0
    means the calendar means nothing to whoever runs this."""

    interval_cv: float | None = None
    """Coefficient of variation of the gaps between transactions. Low means
    scheduled."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None

    # ------------------------------------------------------------------ verdicts

    @property
    def has_diurnal_pattern(self) -> bool:
        """Whether there is a day/night rhythm worth interpreting at all."""
        return self.samples >= MIN_SAMPLES and self.concentration >= MIN_CONCENTRATION

    @property
    def looks_automated(self) -> bool:
        """Regular intervals and no weekly rhythm: a schedule, not a person."""
        if self.samples < MIN_SAMPLES or self.interval_cv is None:
            return False
        return self.interval_cv < AUTOMATION_CV and self.weekend_ratio > 0.8

    @property
    def likely_utc_offset(self) -> int | None:
        """Offset that would put the quiet window over local night.

        ``None`` whenever the pattern is too weak to support one, which is the
        common case and the correct answer for anything scripted.

        The estimate assumes the quiet window is sleep centred near 03:00 local.
        That assumption is worth stating because it is exactly what fails for a
        shift worker, a team spanning zones, or an actor deliberately shifting
        hours --- and none of those look different in this data.
        """
        if not self.has_diurnal_pattern or self.quiet_window_utc is None:
            return None
        start, end = self.quiet_window_utc
        span = (end - start) % 24 or 24
        centre = (start + span / 2) % 24
        offset = round(3 - centre)
        # Normalise to the real range of civil offsets.
        return ((offset + 12) % 24) - 12

    @property
    def offset_uncertainty(self) -> int | None:
        """How wide the plausible band around :attr:`likely_utc_offset` is.

        Not decoration. Validated against synthetic actors with known offsets,
        the point estimate lands two to three hours off, because it rests on
        assuming the quiet window is centred on 03:00 local and real sleep is
        not that tidy. Quoting a bare offset would be exactly the over-precision
        this module exists to avoid, so the band travels with it.

        A wide quiet window localises worse than a narrow one --- twelve quiet
        hours could be centred anywhere within several of them --- so the band
        grows with the excess over a nominal eight-hour night.
        """
        if self.quiet_window_utc is None or self.likely_utc_offset is None:
            return None
        start, end = self.quiet_window_utc
        span = (end - start) % 24 or 24
        return 2 + max(0, round((span - 8) / 2))

    @property
    def offset_range(self) -> tuple[int, int] | None:
        """The band itself, which is what should be quoted rather than a point.

        ``None`` once the band stops excluding anything. A cold-start run on a
        sparse address printed "operating hours consistent with UTC-14 to
        UTC+6" --- twenty hours wide, covering nearly every inhabited
        longitude, presented as a finding with a bullet point beside it.

        Technically honest and practically a lie of emphasis: a reader skims
        the label, not the width. Past :data:`MAX_USEFUL_BAND` hours either
        side, the honest output is nothing rather than a range that rules out
        almost no one.
        """
        offset, band = self.likely_utc_offset, self.offset_uncertainty
        if offset is None or band is None or band > MAX_USEFUL_BAND:
            return None
        return (offset - band, offset + band)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "samples": self.samples,
            "by_hour_utc": list(self.by_hour),
            "by_weekday": list(self.by_weekday),
            "concentration": round(self.concentration, 3),
            "peak_hour_utc": self.peak_hour_utc,
            "quiet_window_utc": list(self.quiet_window_utc) if self.quiet_window_utc else None,
            "weekend_ratio": round(self.weekend_ratio, 3),
            "interval_cv": round(self.interval_cv, 3) if self.interval_cv is not None else None,
            "has_diurnal_pattern": self.has_diurnal_pattern,
            "looks_automated": self.looks_automated,
            "likely_utc_offset": self.likely_utc_offset,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }

    def summary(self) -> str:
        """One paragraph a human can read, phrased so it cannot be over-quoted."""
        if self.samples < MIN_SAMPLES:
            return (
                f"{self.samples} timestamped actions --- too few to say anything "
                f"about timing. {MIN_SAMPLES} is the floor, and below it apparent "
                f"regularity is usually chance."
            )
        if self.looks_automated:
            return (
                f"Activity is evenly spaced (interval CV {self.interval_cv:.2f}) "
                f"and ignores weekends (ratio {self.weekend_ratio:.2f}). This "
                f"looks scheduled rather than human, so no operating hours are "
                f"inferred."
            )
        if not self.has_diurnal_pattern:
            return (
                f"Activity is spread through the day (concentration "
                f"{self.concentration:.2f}); no day/night rhythm to interpret. "
                f"That is normal for contracts, exchange wallets, and anything "
                f"run from more than one place."
            )
        start, end = self.quiet_window_utc or (0, 0)
        observed = (
            f"Quiet between {start:02d}:00 and {end:02d}:00 UTC across "
            f"{self.samples} actions, peak at {self.peak_hour_utc:02d}:00 UTC. "
        )
        band = self.offset_range
        if band is None:
            # The observation stands; the inference does not. Printing a
            # degenerate "UTC+0 to UTC+0" was worse than saying nothing --- it
            # read as a precise answer produced by a calculation that had
            # already given up.
            return observed + (
                "That window is too wide to place anyone: the plausible band "
                "spans more of the clock than it excludes, so no offset is "
                "reported. The activity is real; the location is not inferable "
                "from it."
            )
        low, high = band
        return observed + (
            f"If that gap is sleep, the operator sits somewhere in "
            f"UTC{low:+d} to UTC{high:+d} --- a band, not a point, because the "
            f"estimate assumes a 03:00 local sleep centre and real hours are "
            f"not that tidy. It equally fits a shift pattern, a team in one "
            f"office, or deliberately shifted hours; none of those look "
            f"different in this data."
        )


# --------------------------------------------------------------------- analysis


def _circular_stats(hours: Sequence[int]) -> tuple[float, float]:
    """Circular mean hour and mean resultant length.

    Hours wrap, so the arithmetic mean is not merely imprecise --- it is
    actively wrong in the interesting case. An actor working 22:00--02:00 has
    an arithmetic mean of 12:00, the one hour they are never active.
    """
    if not hours:
        return 0.0, 0.0
    angles = [h * math.tau / 24 for h in hours]
    x = sum(math.cos(a) for a in angles) / len(angles)
    y = sum(math.sin(a) for a in angles) / len(angles)
    magnitude = math.hypot(x, y)
    mean_hour = (math.atan2(y, x) % math.tau) * 24 / math.tau
    return mean_hour, magnitude


def _quiet_window(by_hour: Sequence[int]) -> tuple[int, int] | None:
    """Longest run of below-average hours, wrapping past midnight.

    Wrapping is the point: a sleep window almost always crosses midnight in
    UTC for most of the world, and a scan that stops at hour 23 finds two short
    windows instead of one long one.
    """
    total = sum(by_hour)
    if total == 0:
        return None
    threshold = total / 24 * 0.5

    best_start, best_len = None, 0
    start, length = None, 0
    # Twice around, so a run spanning midnight is seen whole.
    for i in range(48):
        hour = i % 24
        if by_hour[hour] <= threshold:
            if start is None:
                start = hour
            length += 1
            if length > best_len:
                best_len, best_start = length, start
        else:
            start, length = None, 0
    if best_start is None or best_len < 3 or best_len >= 24:
        # Fewer than three hours is noise; twenty-four means no activity at all.
        return None
    return best_start, (best_start + min(best_len, 23)) % 24


class Timed(Protocol):
    """Anything with a time and two ends.

    Both :class:`~chainscope.core.models.Transfer` and
    :class:`~chainscope.core.models.Transaction` satisfy this, and profiling
    accepts either on purpose. A *reverted* transaction moves no value but is
    still an action this key took at a moment --- somebody was awake, signed,
    and broadcast. Dropping those would bias the profile toward the hours in
    which things happened to succeed.
    """

    @property
    def timestamp(self) -> datetime | None: ...
    @property
    def sender(self) -> Any: ...
    @property
    def recipient(self) -> Any: ...


def _keying(transfers: Sequence[Any]) -> Callable[[str], str]:
    """How to compare an address against these transfers --- from their chain.

    With no transfers, or a mix of chains, the address is compared as written:
    a miss rather than a match against somebody else's address.
    """
    chains = {str(t.chain) for t in transfers if getattr(t, "chain", None)}
    if len(chains) != 1:
        return lambda address: address.strip()
    only = next(iter(chains))
    return lambda address: address_key(only, address)


def profile_activity(
    transfers: Iterable[Timed], address: str, *, direction: str = "out"
) -> ActivityProfile:
    """Build a temporal profile from an address's own actions.

    Only actions the address *sent* count by default. Inbound ones are somebody
    else's timing, and folding them in measures the union of two schedules ---
    which is nobody's.
    """
    # Compared the way the transfers' own chain compares. `.lower()` on both
    # sides is right on EVM and asks about a different account on Solana, Sui
    # and Bitcoin --- where it produced an empty series, which `profile` then
    # reports as "not enough timestamps to place anyone".
    rows = list(transfers)
    key = _keying(rows)(address)
    times: list[datetime] = []
    for t in rows:
        if t.timestamp is None:
            continue
        if direction == "out" and not (t.sender and t.sender.key == key):
            continue
        if direction == "in" and not (t.recipient and t.recipient.key == key):
            continue
        times.append(t.timestamp.astimezone(timezone.utc))

    profile = ActivityProfile(address=address, samples=len(times))
    if not times:
        return profile

    times.sort()
    profile.first_seen, profile.last_seen = times[0], times[-1]

    hours = [t.hour for t in times]
    hour_counts = Counter(hours)
    profile.by_hour = tuple(hour_counts.get(h, 0) for h in range(24))
    weekday_counts = Counter(t.weekday() for t in times)
    profile.by_weekday = tuple(weekday_counts.get(d, 0) for d in range(7))

    _, profile.concentration = _circular_stats(hours)
    profile.peak_hour_utc = max(range(24), key=lambda h: profile.by_hour[h])
    profile.quiet_window_utc = _quiet_window(profile.by_hour)

    weekday_total = sum(profile.by_weekday[:5])
    weekend_total = sum(profile.by_weekday[5:])
    # Per-day, or a five-to-two split reads as a weekend lull that is not there.
    weekday_rate = weekday_total / 5
    weekend_rate = weekend_total / 2
    profile.weekend_ratio = weekend_rate / weekday_rate if weekday_rate else 0.0

    if len(times) >= 3:
        gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times) if b > a]
        if len(gaps) >= 2:
            mean = statistics.fmean(gaps)
            profile.interval_cv = statistics.pstdev(gaps) / mean if mean > 0 else None

    return profile


def temporal_attribution(
    profile: ActivityProfile, chain: ChainId | None = None
) -> Attribution | None:
    """Turn a profile into a claim, or return ``None`` if it does not support one.

    Returning ``None`` is the common outcome and the important one. A function
    that always produces an attribution turns every address into an actor with
    a location, which is both wrong and the kind of wrong that gets repeated.

    The claim never exceeds :attr:`Confidence.MEDIUM`. Timing is circumstantial:
    it narrows a hypothesis and cannot confirm one, and a MEDIUM ceiling is what
    stops it being cited as though it had.
    """
    if profile.looks_automated:
        return Attribution(
            label="scheduled or scripted activity",
            category=Category.SERVICE,
            confidence=Confidence.LOW,
            method=Method.HEURISTIC,
            source="chainscope temporal analysis",
            address=profile.address,
            chain=chain,
            rationale=profile.summary(),
        )

    offset = profile.likely_utc_offset
    if offset is None:
        return None

    band = profile.offset_range
    if band is None:
        # A point estimate whose band was too wide to report. Quoting the point
        # alone would be the over-precision this module exists to avoid, and it
        # is what a reader would carry away.
        return None
    low, high = band
    # Confidence rises with both sample size and how sharply the activity
    # clusters. Neither alone is enough: a thousand evenly spread timestamps say
    # nothing, and forty tightly clustered ones are still forty.
    strong = profile.samples >= 200 and profile.concentration >= 0.45
    return Attribution(
        # A band in the label, not a point. Whatever is in the label is what
        # gets quoted, and a bare "UTC+6" would be quoted as though measured.
        label=f"operating hours consistent with UTC{low:+d} to UTC{high:+d}",
        category=Category.SERVICE,
        confidence=Confidence.MEDIUM if strong else Confidence.LOW,
        method=Method.INFERENCE,
        source="chainscope temporal analysis",
        address=profile.address,
        chain=chain,
        rationale=profile.summary(),
    )


class TemporalAnalyzer(Analyzer):
    """Profile when an address acts."""

    name = "temporal"
    version = "1.0"
    description = "Profile an address's operating hours from its own outbound activity"

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, Capability.ADDRESS_HISTORY))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        direction: str = "out",
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not address:
            raise ValueError("temporal profiling needs an `address`")
        if direction not in ("out", "in"):
            raise ValueError(f"direction must be 'out' or 'in', got {direction!r}")

        seed = address_key(ctx.chain, address)
        per_node = ctx.limit("per_node", 1000)
        history, completeness = history_of(
            ctx,
            lambda p: p.address_history(
                ctx.chain, seed, start_block=start_block, end_block=end_block, limit=per_node
            ),
        )
        warnings: list[str] = list(completeness)
        if len(history) >= per_node:
            # A profile built from the most recent N transfers describes the
            # window, not the address. Saying so is the difference between a
            # measurement and an impression.
            warnings.append(
                f"history was capped at {per_node} transfers, so this profile "
                f"describes that window rather than the address's whole life"
            )

        undated = sum(1 for t in history if t.timestamp is None)
        if undated:
            warnings.append(
                f"{undated} of {len(history)} transfers carry no timestamp and "
                f"were excluded; some providers omit them"
            )

        profile = profile_activity(history, seed, direction=direction)
        if profile.samples < MIN_SAMPLES:
            return self._result(
                ctx,
                warnings=(
                    *warnings,
                    f"only {profile.samples} timestamped {direction}bound transfers, "
                    f"below the {MIN_SAMPLES} needed before any pattern claim is made",
                ),
                params={
                    "address": seed,
                    "direction": direction,
                    "start_block": start_block,
                    "end_block": end_block,
                    "per_node": per_node,
                },
                started=started,
            )

        claim = temporal_attribution(profile, ctx.chain)
        findings = [
            Finding(
                title=claim.label if claim else f"no timing pattern for {seed}",
                severity=Severity.INFO,
                detail=profile.summary(),
                data={
                    "address": seed,
                    "samples": profile.samples,
                    "by_hour": list(profile.by_hour),
                    "by_weekday": list(profile.by_weekday),
                    "peak_hour_utc": profile.peak_hour_utc,
                    "quiet_window_utc": profile.quiet_window_utc,
                    "concentration": profile.concentration,
                    "likely_utc_offset": profile.likely_utc_offset,
                    "offset_range": profile.offset_range,
                    "looks_automated": profile.looks_automated,
                    "confidence": claim.confidence.name if claim else None,
                },
            )
        ]
        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={
                "address": seed,
                "direction": direction,
                "start_block": start_block,
                "end_block": end_block,
                "per_node": per_node,
            },
            started=started,
        )
