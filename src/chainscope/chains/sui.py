"""Sui address handling.

Three details that break code written by analogy with EVM, in ascending order of
how quietly they do it.

**Addresses are 32 bytes, not 20.** A Sui address is ``0x`` plus 64 hex
characters. Validation borrowed from an EVM adapter accepts the first 40 and
silently truncates, producing a well-formed address that belongs to nobody.

**Short forms are legal and must be padded.** ``0x2`` and ``0x0000…0002`` are
the same address --- ``0x2`` is the Sui framework package, written that way
everywhere in practice. Comparing the literal strings says they differ, so an
address seen in one form and looked up in the other becomes two entities. That
is exactly the failure that makes a traversal quietly incomplete, so
normalisation pads rather than accepting either form.

**SUI has nine decimals, not eighteen.** The base unit is MIST, and 1 SUI is
1e9 MIST. Reading a balance with 18 assumed makes it appear a billion times
smaller, which is small enough to look like dust and be skipped.

Coin types carry their own structure: ``0x2::sui::SUI`` is package, module,
name. Two coins can share a symbol and differ in package, so the package is
what identifies an asset --- the symbol is a display detail, and treating it as
identity is how a fake token with a real name gets counted as the real one.
"""

from __future__ import annotations

import re

from ..core.chainid import ChainId, Ecosystem
from ..core.models import Address
from .base import ChainAdapter, InvalidAddressError

__all__ = [
    "SUI_DECIMALS",
    "SUI_MAINNET",
    "SUI_TESTNET",
    "SUI_TYPE",
    "SuiAdapter",
    "coin_symbol",
]

#: MIST per SUI. Nine, not eighteen.
SUI_DECIMALS = 9

#: Canonical coin type for the native asset.
SUI_TYPE = "0x2::sui::SUI"

# The `0x` prefix carries no information, so its case is not held
# against the address. The body is hex and case-insensitive anyway.
_ADDRESS = re.compile(r"^0[xX][0-9a-fA-F]{1,64}$")

#: ``package::module::name``. The package is an address and may be short.
# Anchored. Unanchored, "0x2::sui::SUI<T>" matched as plain SUI and
# coin_decimals then handed back nine for a wrapped type that is not SUI at
# all --- a silent order-of-magnitude error on exactly the exotic assets worth
# looking at. Generics are not supported, so they are refused rather than
# quietly discarded.
_COIN_TYPE = re.compile(r"^(0[xX][0-9a-fA-F]{1,64})::([a-zA-Z_]\w*)::([a-zA-Z_]\w*)$")


def normalize_address(raw: str) -> str:
    """Pad a Sui address to its full 32-byte form, lowercased.

    ``0x2`` and its padded form are one address. Without this they are two
    entities, one of which is the framework package that appears in nearly
    every transaction.
    """
    text = raw.strip()
    if not _ADDRESS.match(text):
        raise InvalidAddressError(f"not a Sui address: {raw!r}")
    return "0x" + text[2:].lower().rjust(64, "0")


def coin_symbol(coin_type: str) -> str:
    """Display symbol for a coin type. Never an identity.

    Two coins can share a name and differ in package, and the package is what
    decides which is which. A fake token borrowing a real symbol is a standard
    trick, so this is for showing to a human and nothing else.
    """
    match = _COIN_TYPE.match(coin_type.strip())
    return match.group(3) if match else coin_type


def coin_decimals(coin_type: str) -> int | None:
    """Decimals for a coin type, where they are known without a lookup.

    Only the native asset is answered here. Guessing 9 for an arbitrary coin
    would be wrong for most of them, and a wrong exponent is a silent
    order-of-magnitude error rather than a visible failure --- so an unknown
    coin returns None and the caller has to go and ask.
    """
    try:
        return (
            SUI_DECIMALS
            if normalize_coin_type(coin_type) == normalize_coin_type(SUI_TYPE)
            else None
        )
    except InvalidAddressError:
        return None


def normalize_coin_type(coin_type: str) -> str:
    """Canonical form of a coin type, with the package address padded."""
    match = _COIN_TYPE.match(coin_type.strip())
    if not match:
        raise InvalidAddressError(f"not a Sui coin type: {coin_type!r}")
    package, module, name = match.groups()
    return f"{normalize_address(package)}::{module}::{name}"


class SuiAdapter(ChainAdapter):
    """Address handling for Sui."""

    ecosystem = Ecosystem.SUI
    name = "sui"
    native_symbol = "SUI"
    native_decimals = SUI_DECIMALS

    def normalize(self, raw: str) -> str:
        return normalize_address(raw)

    def is_valid(self, raw: str) -> bool:
        try:
            normalize_address(raw)
        except InvalidAddressError:
            return False
        return True

    def display(self, address: Address) -> str:
        key = getattr(address, "key", str(address))
        # A padded address is 66 characters of mostly zeros; showing the whole
        # thing hides the part that distinguishes it.
        return f"{key[:10]}…{key[-6:]}" if len(key) > 20 else key


#: Sui's CAIP-2 namespace is ``sui``; the reference is the network name rather
#: than a numeric id.
SUI_MAINNET = ChainId("sui", "mainnet")
SUI_TESTNET = ChainId("sui", "testnet")
