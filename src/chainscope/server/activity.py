"""What the server actually did, kept so the page can show it.

Every other part of this package refuses to let an absence pass for a result.
The one place that rule was not enforced is the place it matters most: a page
that fetched an address and drew nothing. That happens when the address has no
transfers, when the provider refused the request, when a rate limit was hit
halfway through, when a page budget ran out, and when the endpoint quietly
served page one again. Five different facts, one empty canvas.

The counts on screen ("22 addresses, 29 flows") are the *outcome*. This is the
*work*: one row per provider read, with which provider answered, how long it
took, how many rows came back, and whether it failed. A reader who sees three
`failed` rows against Blockscout knows the picture is short by whatever those
would have carried; without them the same picture reads as complete.

**Bounded and in memory.** It is a diagnostic view of one run, not a record.
Persisting it would put an investigation's address list on disk in a second
place with different handling from the case file, which is a worse thing to get
wrong than losing a log on restart.

**Addresses are recorded as written, not folded.** The log exists to be
compared against what somebody typed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Literal

__all__ = ["CEILINGS", "Event", "Log", "Outcome", "is_ceiling"]

#: How many reads are kept. A deep expand of twenty addresses at fifteen pages
#: each is three hundred; this holds a whole one of those and then some, which
#: is the span somebody is actually asking about when they ask why the picture
#: looks thin.
CAPACITY = 400

Outcome = Literal["ok", "empty", "more", "capped", "failed"]

#: Refusals that mean "this provider will not page any further", not "this
#: provider is broken". Blockscout stops at ten thousand rows and says so by
#: raising; Etherscan phrases the same ceiling differently. Both leave the
#: answer a **prefix** --- everything fetched is real, and there is more that
#: cannot be reached this way --- which calls for a narrower window or another
#: provider, not a retry. Recording them as failures said the opposite.
#:
#: Found in the read log, on the first case opened after it existed: page
#: eleven of a Lazarus address, red, next to nine pages that had worked.
CEILINGS = (
    "result window is too large",
    "window is too large",
    "page number too large",
    "result set too large",
    "max offset",
)


def is_ceiling(message: str) -> bool:
    """Whether a refusal is a paging limit rather than a fault."""
    low = message.lower()
    return any(mark in low for mark in CEILINGS)


@dataclass(frozen=True)
class Event:
    """One read from a provider.

    `outcome` is the field the page colours on, and the five values are
    deliberately not collapsible into a boolean:

    ``ok``      rows came back and that was the end of them
    ``empty``   the provider answered, and the answer was nothing
    ``more``    a full page --- there is more beyond it
    ``capped``  the provider will not page further. What was fetched is real
                and there is more it cannot reach: the answer is a prefix.
    ``failed``  no answer. Anything drawn from this read is missing rows.

    ``capped`` and ``failed`` are separated because they call for different
    moves --- a narrower window or another provider, against a retry --- and
    because a ceiling is a known property of the endpoint rather than a fault.
    """

    at: float
    provider: str
    chain: str
    what: str
    address: str
    outcome: Outcome
    rows: int
    ms: int
    detail: str = ""


class Log:
    """A bounded ring of `Event`, safe to write from the fetch pool.

    `_fetch_into` runs pages concurrently, so this is written from several
    threads at once. `deque` with a `maxlen` is atomic for append under the
    GIL, but `recent` snapshots under the lock so a reader never sees a
    half-rotated ring.
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._events: deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        provider: str,
        chain: str,
        what: str,
        address: str,
        outcome: Outcome,
        rows: int = 0,
        ms: int = 0,
        detail: str = "",
    ) -> None:
        event = Event(
            at=time.time(),
            provider=provider,
            chain=chain,
            what=what,
            address=address,
            outcome=outcome,
            rows=rows,
            ms=ms,
            # Bounded: a provider that returns an HTML error page would
            # otherwise put a kilobyte of markup in every row of the log.
            detail=detail[:300],
        )
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        """The newest first, as plain dicts."""
        with self._lock:
            events = list(self._events)
        return [asdict(e) for e in reversed(events[-limit:])]

    def summary(self) -> dict[str, int]:
        """Counts by outcome, over everything still held.

        Separate from `recent` because the page shows a short list and needs
        the totals beside it: three failures scrolled off the end of a
        sixty-row view are still three failures, and a summary that only
        covered what was on screen would hide exactly the ones that matter in a
        long run.
        """
        with self._lock:
            events = list(self._events)
        counts = {"ok": 0, "empty": 0, "more": 0, "capped": 0, "failed": 0}
        for event in events:
            counts[event.outcome] = counts.get(event.outcome, 0) + 1
        counts["total"] = len(events)
        return counts

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


#: The server's log. Module-level because `_fetch_page` is a module function
#: reached from a thread pool, and threading a handler through it would put a
#: parameter on the fetch path for the benefit of a display.
LOG = Log()
