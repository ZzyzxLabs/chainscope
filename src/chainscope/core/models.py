"""Normalised on-chain objects.

Every chain adapter converts its native format into these types, so analysis
code never branches on which chain it is looking at. The types are immutable:
an investigation that mutates its own evidence is not reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .chainid import ChainId
from .units import Amount

__all__ = [
    "Account",
    "Address",
    "Block",
    "Transaction",
    "Transfer",
    "TransferKind",
    "TxRef",
]


@dataclass(frozen=True, slots=True)
class Address:
    """A chain-scoped address.

    Two forms are kept deliberately. ``raw`` is exactly what the chain shows and
    is what you submit or display; ``key`` is the comparison form.

    They differ because normalisation is chain-specific in a way that is easy to
    get catastrophically wrong: lowercasing an EVM address is correct and
    harmless, while lowercasing a base58 or bech32 address destroys it. Keeping
    both means a comparison bug cannot silently corrupt what you report.
    """

    chain: ChainId
    raw: str
    key: str

    def __post_init__(self) -> None:
        if not self.raw.strip():
            raise ValueError("address cannot be empty")

    def __str__(self) -> str:
        return self.raw

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Address):
            return NotImplemented
        return self.chain == other.chain and self.key == other.key

    def __hash__(self) -> int:
        return hash((self.chain, self.key))


@dataclass(frozen=True, slots=True)
class TxRef:
    """A pointer to a transaction. Cheap to pass around; carries no payload."""

    chain: ChainId
    hash: str

    def __str__(self) -> str:
        return self.hash


class TransferKind(str, Enum):
    """How value moved.

    ``INTERNAL`` matters more than it looks. Contract-to-contract value movement
    produces no log and no top-level transaction, so a tracer that only reads
    transactions and token transfers misses it entirely --- and it is exactly
    where swap proceeds and withdrawal payouts live.
    """

    NATIVE = "native"
    TOKEN = "token"
    INTERNAL = "internal"
    NFT = "nft"
    FEE = "fee"


@dataclass(frozen=True, slots=True)
class Transfer:
    """One movement of value.

    The unit every analyzer works in. A native send, an ERC-20 transfer, a UTXO
    output, and an internal call all reduce to this.
    """

    chain: ChainId
    tx: TxRef
    sender: Address | None
    recipient: Address | None
    amount: Amount
    kind: TransferKind
    timestamp: datetime | None = None
    block: int | None = None
    index: int = 0
    asset: Address | None = None
    """Token contract, or ``None`` for the chain's native asset."""

    def __post_init__(self) -> None:
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware; use timezone.utc")

    @property
    def is_mint(self) -> bool:
        return self.sender is None

    @property
    def is_burn(self) -> bool:
        return self.recipient is None

    def __str__(self) -> str:
        f = self.sender.raw[:10] + "…" if self.sender else "mint"
        t = self.recipient.raw[:10] + "…" if self.recipient else "burn"
        return f"{self.amount} {f} → {t}"


@dataclass(frozen=True, slots=True)
class Transaction:
    """A transaction, with the transfers it produced."""

    ref: TxRef
    sender: Address | None
    recipient: Address | None
    value: Amount
    timestamp: datetime | None
    block: int | None
    success: bool = True
    fee: Amount | None = None
    nonce: int | None = None
    input_data: str = ""
    transfers: tuple[Transfer, ...] = ()

    @property
    def selector(self) -> str | None:
        """The 4-byte function selector, lowercase and *without* ``0x``.

        Several submission formats require exactly this form, and returning the
        prefixed variant here has caused real rejected answers.
        """
        d = self.input_data
        if not d or len(d) < 10:
            return None
        return d[2:10].lower() if d.startswith("0x") else d[:8].lower()


@dataclass(frozen=True, slots=True)
class Block:
    chain: ChainId
    number: int
    hash: str
    timestamp: datetime
    tx_count: int = 0
    parent: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("block timestamp must be timezone-aware; use timezone.utc")

    @property
    def age(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


@dataclass(frozen=True, slots=True)
class Account:
    """A point-in-time view of an address."""

    address: Address
    balance: Amount | None = None
    tx_count: int | None = None
    is_contract: bool = False
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    tags: frozenset[str] = field(default_factory=frozenset)

    def completeness_check(self, observed_sent: int) -> bool | None:
        """Whether ``observed_sent`` accounts for every transaction this address sent.

        On nonce-based chains the account nonce equals the number of outbound
        transactions, so this is a cheap proof that a paginated history was not
        silently truncated. Skipping it is how an analysis quietly runs on
        partial data and reports a total that is simply too low.

        Returns ``None`` when the chain does not expose a usable nonce.
        """
        if self.tx_count is None:
            return None
        return self.tx_count == observed_sent
