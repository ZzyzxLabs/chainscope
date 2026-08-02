"""Exact on-chain quantities.

Financial forensics with floating point is malpractice. ``0.1 + 0.2 != 0.3``
stops being an academic curiosity the moment you are summing 8,116 ETH across
twelve deposits and reporting the total as evidence.

``Amount`` therefore stores the raw integer in the asset's smallest unit --- wei,
satoshi, or a token's ``10 ** decimals`` --- and treats that integer as the only
source of truth. Decimals exist for display and for parsing human input; they
never feed back into arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

__all__ = ["Amount", "AmountError"]

_MAX_DECIMALS: Final = 36  # far beyond any real asset; guards against nonsense


def _prec_for(value: Decimal | int, decimals: int) -> int:
    """Precision wide enough that scaling never rounds.

    Decimal defaults to 28 significant digits. A wei-denominated balance above
    ~10**28 silently loses its low-order digits under that default --- which is
    to say, the exact figure you are about to report as evidence quietly stops
    being exact. Found by a property test; kept honest by one.
    """
    digits = len(Decimal(value).as_tuple().digits)
    return max(28, digits + decimals + 2)


class AmountError(ValueError):
    """Raised when two amounts cannot be combined, or an input is malformed."""


@dataclass(frozen=True, slots=True, order=False)
class Amount:
    """An exact quantity of one asset.

    Args:
        raw: Value in the asset's smallest indivisible unit. May be negative to
            express a debit or a delta.
        decimals: How many digits the smallest unit sits below the display unit.
            18 for ETH/wei, 8 for BTC/satoshi, 6 for USDC.
        symbol: Display ticker. Purely cosmetic *except* that arithmetic refuses
            to mix different symbols, which is the point.

    Examples:
        >>> Amount(1_000_000_000_000_000_000, 18, "ETH")
        Amount(1 ETH)
        >>> Amount.parse("0.5", 8, "BTC").raw
        50000000
    """

    raw: int
    decimals: int
    symbol: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.raw, int) or isinstance(self.raw, bool):
            raise AmountError(f"raw must be int, got {type(self.raw).__name__}")
        if not 0 <= self.decimals <= _MAX_DECIMALS:
            raise AmountError(f"decimals out of range: {self.decimals}")

    # ---------------------------------------------------------------- builders

    @classmethod
    def parse(cls, value: str | Decimal | int, decimals: int, symbol: str = "") -> Amount:
        """Build from a *display* value such as ``"0.5"`` BTC.

        Rejects inputs with more precision than the asset can represent rather
        than silently truncating --- a truncated amount is a wrong answer that
        looks right.
        """
        d = Decimal(str(value))
        with localcontext() as ctx:
            ctx.prec = _prec_for(d, decimals)
            scaled = d.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise AmountError(
                f"{value} needs more than {decimals} decimals; "
                f"would lose {scaled - scaled.to_integral_value()} of a unit"
            )
        return cls(int(scaled), decimals, symbol)

    @classmethod
    def zero(cls, decimals: int, symbol: str = "") -> Amount:
        return cls(0, decimals, symbol)

    # ---------------------------------------------------------------- accessors

    @property
    def decimal(self) -> Decimal:
        """Display value. For humans and reports; never round-trip through this."""
        with localcontext() as ctx:
            ctx.prec = _prec_for(self.raw, self.decimals)
            return Decimal(self.raw).scaleb(-self.decimals)

    def is_zero(self) -> bool:
        return self.raw == 0

    # ---------------------------------------------------------------- arithmetic

    def _check(self, other: Amount, op: str) -> None:
        if self.decimals != other.decimals or self.symbol != other.symbol:
            raise AmountError(
                f"cannot {op} {self.symbol or '?'}({self.decimals}) and "
                f"{other.symbol or '?'}({other.decimals}) --- convert first"
            )

    def __add__(self, other: Amount) -> Amount:
        self._check(other, "add")
        return Amount(self.raw + other.raw, self.decimals, self.symbol)

    def __sub__(self, other: Amount) -> Amount:
        self._check(other, "subtract")
        return Amount(self.raw - other.raw, self.decimals, self.symbol)

    def __neg__(self) -> Amount:
        return Amount(-self.raw, self.decimals, self.symbol)

    def __abs__(self) -> Amount:
        return Amount(abs(self.raw), self.decimals, self.symbol)

    def __mul__(self, k: int) -> Amount:
        if not isinstance(k, int) or isinstance(k, bool):
            raise AmountError("Amount may only be scaled by an int; use ratio() for rates")
        return Amount(self.raw * k, self.decimals, self.symbol)

    __rmul__ = __mul__

    def ratio(self, other: Amount) -> Decimal:
        """Dimensionless ratio between two amounts of the same asset."""
        self._check(other, "compare")
        if other.raw == 0:
            raise AmountError("division by zero amount")
        return Decimal(self.raw) / Decimal(other.raw)

    # ---------------------------------------------------------------- ordering

    def __lt__(self, other: Amount) -> bool:
        self._check(other, "compare")
        return self.raw < other.raw

    def __le__(self, other: Amount) -> bool:
        self._check(other, "compare")
        return self.raw <= other.raw

    def __gt__(self, other: Amount) -> bool:
        self._check(other, "compare")
        return self.raw > other.raw

    def __ge__(self, other: Amount) -> bool:
        self._check(other, "compare")
        return self.raw >= other.raw

    # ---------------------------------------------------------------- rendering

    def __str__(self) -> str:
        text = format(self.decimal, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text} {self.symbol}".strip()

    def __repr__(self) -> str:
        return f"Amount({self})" if self.symbol else f"Amount({self.decimal}, {self.decimals})"

    def format(self, *, grouped: bool = False, places: int | None = None) -> str:
        """Human-facing rendering.

        Note the default is *ungrouped*: thousands separators are forbidden in
        several submission formats and are a common source of rejected answers.
        """
        d = self.decimal
        if places is not None:
            with localcontext() as ctx:
                ctx.prec = _prec_for(self.raw, self.decimals)
                d = d.quantize(Decimal(1).scaleb(-places))
        text = f"{d:,f}" if grouped else format(d, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text} {self.symbol}".strip()


def total(amounts: list[Amount]) -> Amount:
    """Sum a list, raising on mixed assets instead of quietly producing garbage."""
    if not amounts:
        raise AmountError("cannot total an empty list --- ambiguous asset")
    out = amounts[0]
    for a in amounts[1:]:
        out = out + a
    return out
