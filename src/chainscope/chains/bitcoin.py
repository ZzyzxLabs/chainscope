"""Bitcoin address handling.

The important difference from EVM: **these encodings are case-sensitive**.
Normalisation must not lowercase legacy or nested-SegWit addresses. Bech32 is
specified as lowercase and may be normalised down; base58 must be preserved
byte for byte.
"""

from __future__ import annotations

import re

from ..core.chainid import Ecosystem
from .base import ChainAdapter

__all__ = ["BitcoinAdapter"]

# P2PKH (1…) and P2SH (3…). Base58 excludes 0, O, I, l precisely because they
# are visually ambiguous.
_BASE58 = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
# Bech32 / bech32m: v0 (bc1q…) and v1 taproot (bc1p…). Excludes 1, b, i, o.
_BECH32 = re.compile(r"^bc1[02-9ac-hj-np-z]{7,87}$")
_TXID = re.compile(r"^[0-9a-fA-F]{64}$")


class BitcoinAdapter(ChainAdapter):
    ecosystem = Ecosystem.UTXO
    native_symbol = "BTC"
    native_decimals = 8

    def normalize(self, raw: str) -> str:
        """Preserve case for base58; lowercase only bech32.

        Lowercasing a base58 address produces a string that is either invalid or
        --- far worse --- a *different valid address*. Nothing downstream can
        detect that, so it is handled here and nowhere else.
        """
        s = raw.strip()
        if s.lower().startswith("bc1"):
            return s.lower()
        return s

    def is_valid(self, raw: str) -> bool:
        s = raw.strip()
        return bool(_BASE58.match(s) or _BECH32.match(s.lower()))

    def is_valid_tx(self, raw: str) -> bool:
        """Bitcoin txids carry no ``0x`` prefix, unlike EVM hashes."""
        return bool(_TXID.match(raw.strip()))

    @staticmethod
    def address_kind(raw: str) -> str:
        s = raw.strip()
        if s.startswith("1"):
            return "p2pkh"
        if s.startswith("3"):
            return "p2sh"
        low = s.lower()
        if low.startswith("bc1p"):
            return "p2tr"
        if low.startswith("bc1q"):
            return "p2wpkh" if len(low) == 42 else "p2wsh"
        return "unknown"
