"""Chain identity.

A bare ``"eth"`` string is ambiguous the moment you work across ecosystems: is
it Ethereum the network, ether the asset, or Ethereum Classic? chainscope uses
`CAIP-2 <https://chainagnostic.org/CAIPs/caip-2>`_ identifiers instead::

    eip155:1                                       Ethereum mainnet
    eip155:56                                      BNB Smart Chain
    bip122:000000000019d6689c085ae165831e93        Bitcoin mainnet
    solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp        Solana mainnet

The payoff is that anyone can add a chain without asking us to mint an alias:
the namespace already exists and is standardised.

Short aliases (``eth``, ``btc``) remain available for humans at the CLI, but
they resolve to a ``ChainId`` immediately and never travel further inward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = ["ChainId", "Ecosystem", "UnknownChainError"]

_CAIP2 = re.compile(r"^(?P<ns>[-a-z0-9]{3,8}):(?P<ref>[-_a-zA-Z0-9]{1,32})$")


class UnknownChainError(KeyError, ValueError):
    """Raised when an alias cannot be resolved to a chain.

    Both bases, on purpose. ``KeyError`` is what it has always been and existing
    handlers still catch it. ``ValueError`` is what it actually *is* --- a value
    that could not be parsed, not a missing key --- and it is what the layers
    that validate user input catch: the CLI's tag command turns a ``ValueError``
    into an exit code and a message, and without this the same input would
    escape as an unhandled exception.

    ``__str__`` is overridden because ``KeyError`` renders its argument with
    ``repr``, so a carefully worded message came out wrapped in quotes.
    """

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class Ecosystem(str, Enum):
    """Family of chains sharing an address format and transaction model.

    Adapters are written per ecosystem, not per chain --- one EVM adapter serves
    every ``eip155:*`` network.
    """

    EVM = "eip155"
    UTXO = "bip122"
    SOLANA = "solana"
    TRON = "tron"
    # Sui's CAIP-2 reference is a network name ("mainnet"), not a numeric id,
    # which is why nothing here can assume `reference` parses as an integer.
    SUI = "sui"
    COSMOS = "cosmos"

    @property
    def namespace(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ChainId:
    """A CAIP-2 chain identifier."""

    namespace: str
    reference: str

    def __post_init__(self) -> None:
        if not _CAIP2.match(f"{self.namespace}:{self.reference}"):
            raise ValueError(f"not a valid CAIP-2 id: {self.namespace}:{self.reference}")

    @classmethod
    def parse(cls, text: str) -> ChainId:
        m = _CAIP2.match(text.strip())
        if not m:
            raise ValueError(f"not a valid CAIP-2 id: {text!r}")
        return cls(m.group("ns"), m.group("ref"))

    @classmethod
    def evm(cls, chain_id: int) -> ChainId:
        return cls("eip155", str(chain_id))

    @property
    def ecosystem(self) -> Ecosystem | None:
        try:
            return Ecosystem(self.namespace)
        except ValueError:
            return None

    @property
    def evm_chain_id(self) -> int | None:
        """Numeric chain id, for EVM chains only."""
        return int(self.reference) if self.namespace == "eip155" else None

    def __str__(self) -> str:
        return f"{self.namespace}:{self.reference}"


# ---------------------------------------------------------------- well-known

ETHEREUM = ChainId.evm(1)
OPTIMISM = ChainId.evm(10)
BSC = ChainId.evm(56)
GNOSIS = ChainId.evm(100)
POLYGON = ChainId.evm(137)
BASE = ChainId.evm(8453)
ARBITRUM = ChainId.evm(42161)
AVALANCHE = ChainId.evm(43114)
LINEA = ChainId.evm(59144)
SCROLL = ChainId.evm(534352)
SEPOLIA = ChainId.evm(11155111)

BITCOIN = ChainId("bip122", "000000000019d6689c085ae165831e93")
LITECOIN = ChainId("bip122", "12a765e31ffd4059bada1e25190f6e98")
SOLANA = ChainId("solana", "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
TRON = ChainId("tron", "0x2b6653dc")

#: What a chain's gas token is called, where it differs from the ecosystem
#: default. EVM chains all share one adapter and it declares ``ETH``, which is
#: correct for exactly one of them --- so a BSC native transfer came back
#: denominated in ETH, and a Polygon one too. The amount was right and the unit
#: was wrong, which is worse than an obvious failure: the number reads fine.
#:
#: Keyed by chain rather than by adapter because that is where the fact lives.
#: Anything absent falls back to its adapter's declaration.
NATIVE_SYMBOLS: dict[ChainId, str] = {
    BSC: "BNB",
    POLYGON: "POL",
    AVALANCHE: "AVAX",
    GNOSIS: "XDAI",
}


def native_symbol(chain: ChainId, default: str = "") -> str:
    """The gas token's ticker for ``chain``.

    ``default`` is returned for anything not listed, which for EVM means ETH ---
    correct for mainnet and every rollup that settles in ether.
    """
    return NATIVE_SYMBOLS.get(chain, default)


#: Human-friendly aliases accepted at the CLI boundary only.
ALIASES: dict[str, ChainId] = {
    "eth": ETHEREUM,
    "ethereum": ETHEREUM,
    "mainnet": ETHEREUM,
    "op": OPTIMISM,
    "optimism": OPTIMISM,
    "bsc": BSC,
    "bnb": BSC,
    "binance": BSC,
    "gnosis": GNOSIS,
    "xdai": GNOSIS,
    "polygon": POLYGON,
    "matic": POLYGON,
    "base": BASE,
    "arb": ARBITRUM,
    "arbitrum": ARBITRUM,
    "avax": AVALANCHE,
    "avalanche": AVALANCHE,
    "linea": LINEA,
    "scroll": SCROLL,
    "sepolia": SEPOLIA,
    "btc": BITCOIN,
    "bitcoin": BITCOIN,
    "ltc": LITECOIN,
    "litecoin": LITECOIN,
    "sol": SOLANA,
    "solana": SOLANA,
    "trx": TRON,
    "tron": TRON,
}


def resolve(text: str) -> ChainId:
    """Resolve a CLI-facing alias or a CAIP-2 string to a :class:`ChainId`."""
    t = text.strip().lower()
    if t in ALIASES:
        return ALIASES[t]
    if ":" in t:
        return ChainId.parse(text)
    if t.isdigit():
        return ChainId.evm(int(t))
    raise UnknownChainError(
        f"unknown chain {text!r}; try a CAIP-2 id or one of: {', '.join(sorted(ALIASES))}"
    )
