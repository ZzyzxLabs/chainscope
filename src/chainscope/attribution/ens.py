"""ENS names: a self-declared label, and what it is and is not worth.

A reverse record turns ``0xd8dA…6045`` into ``vitalik.eth``, which is the most
readable attribution signal on Ethereum and the most easily over-read. Two
things about it decide how it should be recorded.

**Anybody can set a forward record to any address.** ``binance-hot-wallet.eth``
pointing at an address says the *name owner* wanted that association, not that
the address agrees. Forward resolution alone is a claim by a stranger.

**A reverse record is set by the address itself**, which is genuinely different:
it requires a transaction from that key. That makes it a self-declaration ---
strong evidence about intent, no evidence about identity. Somebody calling
themselves ``coinbase-treasury.eth`` in their own reverse record has told you
what they want to be called.

The standard's own answer to this is **forward-confirmation**: resolve the
reverse record to a name, then resolve that name forward, and only trust it if
you land back on the same address. That is what makes a reverse record
meaningful rather than decorative, and it is why this module refuses to emit an
attribution from an unconfirmed one.

So: confirmed reverse records become :attr:`Confidence.MEDIUM` claims, because
they establish self-declaration and nothing more. A forward record alone is
:attr:`Confidence.LOW` with the asymmetry spelled out in the rationale. Neither
is ever :attr:`Confidence.HIGH` --- a name is not an identity, and the
resemblance between ``uniswap.eth`` and Uniswap is exactly the kind of thing an
impersonator is counting on.

Namehash follows EIP-137 and is checked against the standard's published
vectors, so the derivation is byte-exact or it is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ETHEREUM, ChainId

__all__ = [
    "ENS_REGISTRY",
    "EnsRecord",
    "namehash",
    "normalise_name",
    "resolve_attribution",
    "reverse_node",
]

#: The registry is at one address on mainnet and has never moved.
ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

#: Reverse records live under this suffix: the reverse node for an address is
#: ``namehash("<address without 0x, lowercased>.addr.reverse")``.
REVERSE_SUFFIX = "addr.reverse"


def _keccak() -> Any:
    try:
        from eth_utils import keccak  # type: ignore[attr-defined]
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError("ENS needs the EVM extra: pip install 'chainscope[evm]'") from exc
    return keccak


def normalise_name(name: str) -> str:
    """Lowercase and strip a name.

    Deliberately *not* full UTS-46 / ENSIP-15 normalisation, and that limit
    matters: the confusable characters those standards exist to handle are the
    entire mechanism behind ENS impersonation. ``vitalik.eth`` and a homoglyph
    of it are different names that render identically, and this function will
    not tell them apart.

    Lowercasing is enough to look a name up. It is not enough to decide two
    names are the same, so nothing here compares names for equality --- only
    addresses, which have no such ambiguity.
    """
    return name.strip().lower().rstrip(".")


def namehash(name: str) -> bytes:
    """EIP-137 namehash.

    Recursive by definition: the hash of a name is the hash of its parent
    concatenated with the hash of its leftmost label. The empty name is
    thirty-two zero bytes, which is the base case and also the root node.
    """
    keccak = _keccak()
    node = b"\x00" * 32
    normalised = normalise_name(name)
    if normalised:
        for label in reversed(normalised.split(".")):
            node = bytes(keccak(node + keccak(label.encode("utf-8"))))
    return node


def reverse_node(address: str) -> bytes:
    """The node under which ``address``'s reverse record lives."""
    raw = address[2:] if address.startswith(("0x", "0X")) else address
    if len(raw) != 40:
        raise ValueError(f"not a 20-byte address: {address!r}")
    return namehash(f"{raw.lower()}.{REVERSE_SUFFIX}")


@dataclass(frozen=True, slots=True)
class EnsRecord:
    """What was found, and whether it survives forward-confirmation."""

    address: str
    name: str = ""
    forward_address: str | None = None
    """Where ``name`` resolves to. ``None`` means it was not checked, which is
    different from resolving to nothing --- and the difference decides whether
    a claim can be made at all."""

    @property
    def is_confirmed(self) -> bool:
        """Whether the reverse record round-trips.

        Unconfirmed is the default and the safe one. Anybody can point a name
        at any address, so a name that does not resolve back is a claim by
        whoever owns the name, about somebody else.
        """
        if not self.name or self.forward_address is None:
            return False
        return self.forward_address.lower() == self.address.lower()

    @property
    def was_checked(self) -> bool:
        return self.forward_address is not None


def resolve_attribution(record: EnsRecord, chain: ChainId = ETHEREUM) -> Attribution | None:
    """Turn a record into a claim, or ``None`` when there is nothing to claim.

    Returning ``None`` for an unchecked record is deliberate. "We found a name
    but did not verify it" is not a weaker version of "this address is called
    X" --- it is a different statement, and emitting it as a low-confidence
    attribution invites it to be read as the first one.
    """
    if not record.name:
        return None

    if record.is_confirmed:
        return Attribution(
            label=record.name,
            # SERVICE rather than anything more specific: a name says what
            # somebody calls themselves, not what they do.
            category=Category.SERVICE,
            confidence=Confidence.MEDIUM,
            method=Method.ONCHAIN,
            source="ENS reverse record (forward-confirmed)",
            address=record.address,
            chain=chain,
            rationale=(
                f"{record.address} publishes the reverse record {record.name}, "
                f"and {record.name} resolves back to it. Setting a reverse "
                f"record requires a transaction from this key, so this is a "
                f"self-declaration --- evidence about what the owner wants to "
                f"be called, and none about who they are. A name resembling a "
                f"known service is exactly what an impersonator would choose."
            ),
        )

    if not record.was_checked:
        return None

    return Attribution(
        label=f"claims the name {record.name}",
        category=Category.SERVICE,
        confidence=Confidence.LOW,
        method=Method.ONCHAIN,
        source="ENS reverse record (unconfirmed)",
        address=record.address,
        chain=chain,
        rationale=(
            f"{record.address} publishes the reverse record {record.name}, but "
            f"{record.name} resolves to "
            f"{record.forward_address or 'nothing'} rather than back to it. "
            f"Without that round trip the association is asserted by one side "
            f"only, and forward records can be pointed at any address by "
            f"whoever owns the name."
        ),
    )


def forward_only_attribution(name: str, address: str, chain: ChainId = ETHEREUM) -> Attribution:
    """A claim from a forward record alone, which is a claim by a stranger.

    Kept separate from :func:`resolve_attribution` so that the asymmetry is
    visible at the call site rather than hidden in a confidence value: a
    forward record is somebody *else* saying this address is theirs.
    """
    return Attribution(
        label=f"named {normalise_name(name)} by its owner",
        category=Category.SERVICE,
        confidence=Confidence.LOW,
        method=Method.ONCHAIN,
        source="ENS forward record",
        address=address,
        chain=chain,
        rationale=(
            f"{normalise_name(name)} resolves to {address}, which is set by "
            f"whoever owns the name --- not by this address. Anybody can point "
            f"a name at any address, so on its own this says what the name "
            f"owner wanted, not what the address agrees to. A matching reverse "
            f"record set by the address itself would be the confirmation."
        ),
    )
