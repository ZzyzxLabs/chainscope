"""EVM address handling.

Serves every ``eip155:*`` network. Adding a new EVM chain is a ``ChainId``, not
a new adapter.
"""

from __future__ import annotations

import re

from ..core.chainid import Ecosystem
from ..core.models import Address
from .base import ChainAdapter

__all__ = ["EvmAdapter", "is_checksum_valid", "to_checksum"]

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _keccak(data: bytes) -> bytes:
    """Keccak-256.

    Prefers ``eth_utils`` when the ``evm`` extra is installed, so we agree
    exactly with the rest of the ecosystem. Falls back to ``pycryptodome``.
    Note that this is *not* SHA3-256 --- Ethereum uses the original Keccak
    padding, and using hashlib's sha3_256 here produces plausible-looking
    checksums that are all wrong.
    """
    try:
        from eth_utils import keccak as _k  # type: ignore[attr-defined]

        return bytes(_k(data))
    except ImportError:
        try:
            from Crypto.Hash import keccak as _pyc

            h = _pyc.new(digest_bits=256)
            h.update(data)
            return h.digest()
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EIP-55 checksums need keccak-256. Install chainscope[evm]."
            ) from exc


def to_checksum(address: str) -> str:
    """EIP-55 mixed-case checksum form."""
    if not _ADDRESS.match(address):
        raise ValueError(f"{address!r} is not an EVM address")
    body = address[2:].lower()
    digest = _keccak(body.encode()).hex()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(body)
    )


def is_checksum_valid(address: str) -> bool:
    """Whether a mixed-case address passes EIP-55.

    All-lower and all-upper forms are valid addresses carrying no checksum, so
    they return ``True``; only genuinely mixed case is verified.
    """
    if not _ADDRESS.match(address):
        return False
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    return to_checksum(address) == address


class EvmAdapter(ChainAdapter):
    ecosystem = Ecosystem.EVM
    native_symbol = "ETH"
    native_decimals = 18

    def normalize(self, raw: str) -> str:
        """Lowercase.

        Safe here and only here: EVM addresses are hex, so case carries no
        information beyond the optional EIP-55 checksum.
        """
        return raw.strip().lower()

    def is_valid(self, raw: str) -> bool:
        return bool(_ADDRESS.match(raw.strip()))

    def is_valid_tx(self, raw: str) -> bool:
        return bool(_TX_HASH.match(raw.strip()))

    def display(self, address: Address) -> str:
        try:
            return to_checksum(address.raw)
        except (ValueError, RuntimeError):
            return address.raw
