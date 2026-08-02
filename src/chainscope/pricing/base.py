"""Historical exchange rates.

Needed for cross-chain matching: funds enter a service on one chain and leave on
another, and the only thing linking the two sides is that the amounts correspond
at the rate prevailing at that moment.

One caveat governs this whole module, and it is stated here because getting it
wrong silently breaks the analysis that depends on it:

**A spot rate is not the rate a service actually gave.** Services quote with a
spread and take a fee. Searching for the spot-equivalent amount finds nothing,
and the natural response --- widening the tolerance --- floods the results with
unrelated transactions. The fix is to calibrate the effective discount from
confirmed cases and search around *that*; see
:class:`~chainscope.analysis.xchain.CrossChainMatcher`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = ["PriceSource", "Quote", "RateError"]


class RateError(RuntimeError):
    """No rate is available for that pair at that time."""


@dataclass(frozen=True, slots=True)
class Quote:
    """A rate, and how it was obtained."""

    base: str
    quote: str
    rate: Decimal
    at: datetime
    source: str
    derivation: str = "direct"
    """``direct``, ``inverted``, or ``via USDT`` --- a triangulated rate carries
    two spreads instead of one, which matters when you are reasoning about how
    wide a search window needs to be."""

    def convert(self, amount: Decimal) -> Decimal:
        return amount * self.rate

    def __str__(self) -> str:
        return f"{self.base}/{self.quote} = {self.rate} ({self.derivation}, {self.source})"


class PriceSource(ABC):
    """Base class for rate providers."""

    name: str = "unnamed"

    def is_offline(self) -> bool:
        """Whether this source can answer without network access.

        A method rather than an attribute because for cached sources the answer
        depends on whether the cache has actually been populated.
        """
        return False

    @abstractmethod
    def rate(self, base: str, quote: str, at: datetime) -> Quote:
        """Rate at a point in time. Raise :class:`RateError` if unavailable.

        Returning a stale or approximate rate silently would be worse than
        failing: the caller is about to use it to decide which of several
        transactions is the right one.
        """

    def convert(self, amount: Decimal, base: str, quote: str, at: datetime) -> Decimal:
        return self.rate(base, quote, at).convert(amount)
