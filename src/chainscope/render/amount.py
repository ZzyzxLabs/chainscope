"""Rendering an exact integer amount as something a person can read.

One function, because it had three callers importing it out of
``render/dashboard.py`` --- a module that also imports a graph renderer, so the
third caller closed an import cycle. A formatter used by the dashboard, the flow
graph and an analyzer belongs to none of them.

The behaviour it protects is small and was wrong twice.

**Never a float.** These are integers in an asset's smallest unit, and one
ETH transfer is already 1e18 wei. `float` loses the low digits silently and the
result looks like a number.

**Significant fraction digits, not fraction digits.** Cutting at a fixed
position turned one wei into ``0.000000`` and 0.000012345 ETH into
``0.000012``. The first reads as *nothing moved*, which is exactly the wrong
thing to say about a peel chain or an address-poisoning transfer, where the dust
amount is the entire signal.

**Decimals are passed, never assumed.** Rendering a six-decimal token at
eighteen puts 1,000 USDC on the screen as ``0.000000``, which is how this
function's caller was wrong for as long as it defaulted.
"""

from __future__ import annotations

__all__ = ["PLACES", "human"]

#: Fraction digits kept once something is actually visible.
PLACES = 6


def human(raw: str, decimals: int | None = 18) -> str:
    """An exact amount, readable.

    ``decimals=None`` means the caller does not know, and the raw integer is
    returned marked as raw rather than scaled by a guess. That distinction is
    the difference between a figure that is wrong by a factor of a million and
    one that is visibly unscaled.
    """
    if decimals is None:
        return f"{int(raw):,} raw"
    negative = raw.startswith("-")
    digits = (raw[1:] if negative else raw).rjust(decimals + 1, "0")
    whole = digits[: len(digits) - decimals] or "0"
    frac = digits[len(digits) - decimals :].rstrip("0") if decimals else ""
    if frac:
        keep = PLACES
        if int(whole) == 0:
            # The leading zeros of the fraction are counted separately from the
            # digits kept, so a small number stays small rather than becoming
            # none. One wei renders as 0.000000000000000001, not as zero.
            keep += len(frac) - len(frac.lstrip("0"))
        frac = frac[:keep]
    grouped = f"{int(whole):,}"
    return ("-" if negative else "") + grouped + (f".{frac}" if frac else "")
