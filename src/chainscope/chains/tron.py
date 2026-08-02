"""Tron address handling.

Tron addresses come in two forms for the same account, and confusing them is a
common source of wrong answers:

* **Base58Check** --- ``T…``, 34 characters, what users and explorers show.
* **Hex** --- ``41`` followed by 40 hex characters, what contract event logs
  contain.

An address in a TRC-20 ``Transfer`` log is the hex form; comparing it against a
``T…`` address as strings finds nothing, and the natural conclusion --- "these
funds did not go there" --- is wrong.

:func:`hex_to_base58` and :func:`base58_to_hex` exist so that comparison happens
in one form deliberately rather than by accident.

Tron matters disproportionately in stablecoin investigations: it carries the
largest USDT volume of any chain.
"""

from __future__ import annotations

import re

from ..core.chainid import Ecosystem
from .base import ChainAdapter

__all__ = ["TronAdapter", "base58_to_hex", "hex_to_base58"]

_BASE58 = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_HEX = re.compile(r"^(41)?[0-9a-fA-F]{40}$")
_TXID = re.compile(r"^[0-9a-fA-F]{64}$")

#: Mainnet address prefix byte. 0x41 renders as a leading "T" in base58check.
ADDRESS_PREFIX = "41"


def hex_to_base58(hex_address: str) -> str:
    """Convert ``41…`` (or a bare 40-hex EVM-style address) to ``T…``."""
    try:
        import base58
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Tron address conversion needs base58. Install chainscope[bitcoin] "
            "or chainscope[solana]."
        ) from exc
    h = hex_address.lower().removeprefix("0x")
    if len(h) == 40:
        h = ADDRESS_PREFIX + h
    if not h.startswith(ADDRESS_PREFIX) or len(h) != 42:
        raise ValueError(f"{hex_address!r} is not a Tron hex address")
    return str(base58.b58encode_check(bytes.fromhex(h)).decode())


def base58_to_hex(address: str, *, evm_style: bool = False) -> str:
    """Convert ``T…`` to hex.

    ``evm_style`` drops the ``41`` prefix and adds ``0x``, which is the form
    that appears inside contract event log topics.
    """
    try:
        import base58
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Tron address conversion needs base58") from exc
    raw = base58.b58decode_check(address.strip()).hex()
    return f"0x{raw[2:]}" if evm_style else raw


class TronAdapter(ChainAdapter):
    ecosystem = Ecosystem.TRON
    native_symbol = "TRX"
    native_decimals = 6

    def normalize(self, raw: str) -> str:
        """Canonicalise to the base58 form, preserving case.

        Hex is folded into base58 so that a log-derived address and a
        user-supplied one compare equal. Base58 case is preserved because
        base58check is case-sensitive.
        """
        s = raw.strip()
        if _HEX.match(s) or s.lower().startswith("0x"):
            try:
                return hex_to_base58(s)
            except (ValueError, RuntimeError):
                return s.lower()
        return s

    def is_valid(self, raw: str) -> bool:
        s = raw.strip()
        return bool(_BASE58.match(s) or _HEX.match(s.removeprefix("0x")))

    def is_valid_tx(self, raw: str) -> bool:
        """Tron txids carry no 0x prefix."""
        return bool(_TXID.match(raw.strip()))
