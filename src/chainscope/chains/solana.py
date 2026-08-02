"""Solana address handling.

The one thing that matters here: **base58 is case-sensitive**. Solana addresses
are base58-encoded 32-byte public keys, so lowercasing one produces a string
that is either invalid or --- worse --- a different valid address. Nothing
downstream can detect the difference.

Validation checks the decoded length rather than trusting the string length,
because base58 output length varies with the leading byte: a key with leading
zero bytes encodes shorter, which is why the accepted range is 32-44 characters
for what is always exactly 32 bytes.
"""

from __future__ import annotations

import re

from ..core.chainid import Ecosystem
from .base import ChainAdapter

__all__ = ["SolanaAdapter"]

# Base58 alphabet: no 0, O, I, or l -- excluded precisely because they are
# visually ambiguous, which is also why homoglyph-substituted addresses are
# such an effective phishing technique on chains that use it.
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{86,88}$")


def _decoded_length(address: str) -> int | None:
    """Decoded byte length, or None if the string is not base58.

    Uses the ``base58`` package when the extra is installed. Without it, falls
    back to the character-length check, which accepts a small number of strings
    a real decoder would reject -- acceptable for a validity screen, not for
    anything that then treats the result as a key.
    """
    try:
        import base58
    except ImportError:
        return None
    try:
        return len(base58.b58decode(address))
    except Exception:
        return -1


class SolanaAdapter(ChainAdapter):
    ecosystem = Ecosystem.SOLANA
    native_symbol = "SOL"
    native_decimals = 9

    def normalize(self, raw: str) -> str:
        """Strip whitespace only.

        Case is data here. Lowercasing a Solana address destroys it, and the
        result may still be a valid address belonging to someone else.
        """
        return raw.strip()

    def is_valid(self, raw: str) -> bool:
        s = raw.strip()
        if not _BASE58.match(s):
            return False
        decoded = _decoded_length(s)
        # None means no decoder available; fall back to the pattern check.
        return decoded is None or decoded == 32

    def is_valid_tx(self, raw: str) -> bool:
        """Solana signatures are 64 bytes, base58, with no prefix."""
        s = raw.strip()
        if not _SIGNATURE.match(s):
            return False
        decoded = _decoded_length(s)
        return decoded is None or decoded == 64
