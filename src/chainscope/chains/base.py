"""Chain adapters: native formats in, domain model out.

Adapters are written per **ecosystem**, not per chain. One EVM adapter serves
every ``eip155:*`` network; adding Base or Scroll is a registry entry, not new
code. A new adapter is only warranted by a genuinely different address format or
transaction model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.chainid import ChainId, Ecosystem
from ..core.models import Address, Transfer

__all__ = ["ChainAdapter", "InvalidAddressError"]


class InvalidAddressError(ValueError):
    """A string is not a valid address on this chain."""


class ChainAdapter(ABC):
    """Normalises one ecosystem's data."""

    ecosystem: Ecosystem
    native_symbol: str = ""
    native_decimals: int = 18

    @abstractmethod
    def normalize(self, raw: str) -> str:
        """Return the comparison key for an address.

        **Read this before implementing.** Lowercasing an EVM address is correct
        and lossless. Lowercasing a base58 or bech32 address *destroys* it,
        because those encodings are case-sensitive --- ``1A1zP1eP…`` and
        ``1a1zp1ep…`` are not the same address, and one of them does not exist.

        Whatever this returns is what every equality check, set membership test,
        and clustering algorithm uses. An error here does not raise; it silently
        makes two different addresses look identical, or one address look like
        two. Both produce confidently wrong analyses.
        """

    @abstractmethod
    def is_valid(self, raw: str) -> bool:
        """Whether ``raw`` is well-formed on this chain."""

    def address(self, chain: ChainId, raw: str) -> Address:
        raw = raw.strip()
        if not self.is_valid(raw):
            raise InvalidAddressError(f"{raw!r} is not a valid {self.ecosystem.name} address")
        return Address(chain=chain, raw=raw, key=self.normalize(raw))

    def parse_transfers(self, chain: ChainId, raw_tx: dict[str, Any]) -> list[Transfer]:
        """Extract value movements from a native transaction payload."""
        return []

    def display(self, address: Address) -> str:
        """How to show this address to a human, or submit it.

        Distinct from ``normalize`` on purpose: EVM tooling often displays the
        EIP-55 checksummed form while comparing lowercase.
        """
        return address.raw
