"""What a claim typed into the web form is allowed to assert.

`docs/data-sources.md` says the confidence ceilings are enforced in code
rather than merely documented, and that only a published legal fact --- a
sanctions designation --- may assert CERTAIN. The web form sat outside that
rule: a click could write CERTAIN with an empty rationale, and in the store
it then sat level with an OFAC listing.

The form even labels the field "why --- required below medium" and then did
not require it.
"""

from __future__ import annotations

import pytest

from chainscope.core.attribution import Confidence
from chainscope.server.local import _browser_ceiling


def test_certain_is_refused_however_it_is_justified() -> None:
    """A person reading a page is making a judgement, not stating a fact."""
    assert _browser_ceiling(Confidence.CERTAIN, "I am completely sure") <= Confidence.HIGH


def test_above_medium_needs_a_reason() -> None:
    assert _browser_ceiling(Confidence.HIGH, "") == Confidence.MEDIUM


def test_a_reason_earns_high() -> None:
    assert _browser_ceiling(Confidence.HIGH, "named in the bridge contract") == Confidence.HIGH


@pytest.mark.parametrize("level", [Confidence.SPECULATIVE, Confidence.LOW, Confidence.MEDIUM])
def test_weaker_claims_pass_through_untouched(level: Confidence) -> None:
    """The ceiling lowers; it must never raise, and must not demand a reason
    for a claim that is already modest."""
    assert _browser_ceiling(level, "") == level


def test_it_never_raises() -> None:
    for level in Confidence:
        for reason in ("", "because"):
            assert _browser_ceiling(level, reason) <= level


def test_the_downgrade_is_reported_not_silent() -> None:
    """A silent downgrade is its own defect: the writer believes the store
    holds CERTAIN and it does not."""
    from pathlib import Path

    handler = Path("src/chainscope/server/local.py").read_text()
    assert '"downgraded"' in handler
    assert "only a published legal fact may assert CERTAIN" in handler
