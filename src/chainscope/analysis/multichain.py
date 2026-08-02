"""The same address on several chains, and what that is worth as evidence.

EVM contract addresses are derived, not chosen. ``CREATE`` takes
``keccak(rlp([deployer, nonce]))[12:]``, and nothing in that expression mentions
the chain --- so one key deploying its first contract produces the *same*
address on Ethereum, BSC, Base, and Arbitrum. An actor who wants their
infrastructure at matching addresses everywhere gets it for free by keeping the
nonce aligned.

That gives an investigator two things a plain address lookup does not.

**Corroboration.** A contract at one address on four chains, each deployed by
the same key at the same nonce, is one operator rather than four coincidences.
The addresses agreeing is not itself the evidence --- derivation guarantees
that --- the evidence is the *deployer* being the same, which is what this
module reconstructs.

**Prediction.** Recovering the nonce that produced a known contract tells you
what else that key deployed, including contracts nobody has looked at yet.
Rehearsal deployments at earlier nonces are a recurring pattern: an actor tests
at nonce 0 and executes at nonce 3, and the test contract is often still
sitting there, unexamined.

**The limits, which matter more than the technique.**

Address collision across chains proves nothing on its own. Every EOA exists at
its address on every EVM chain by construction, and so does every counterfactual
contract address. Finding "the same address" on six networks is the default
state of the world, not a finding.

``CREATE2`` breaks the nonce relationship entirely: the address depends on a
salt and the init code, so a deployer can produce addresses in any order and at
any time. It is derived here too, but it says nothing about sequence.

And a matching address says nothing about matching *code*. Two chains can hold
different contracts at one address if the deployer used different init code, and
confirming they are the same needs the code compared, not the address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "Deployment",
    "MultiChainPresence",
    "confirm_deployments",
    "create2_address",
    "create_address",
    "deployments_for",
    "recover_nonce",
]

#: How far to search when recovering a nonce. A deployer that has published more
#: than this is a factory or a long-lived operator, and for either the nonce is
#: not the interesting fact.
DEFAULT_NONCE_SEARCH = 256


def _eth() -> Any:
    try:
        # eth_utils re-exports these from its package root, which is its
        # documented surface, but ships no __all__ marking them as public --- so
        # a strict checker calls them implicit. They are not.
        from eth_utils import (  # type: ignore[attr-defined]
            keccak,
            to_checksum_address,
        )
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            "address derivation needs the EVM extra: pip install 'chainscope[evm]'"
        ) from exc
    return keccak, to_checksum_address


def _rlp(item: bytes | list[Any]) -> bytes:
    """Minimal RLP encoder for the two shapes CREATE needs.

    Written out rather than pulled in: the full ``rlp`` package is a dependency
    for twenty lines, and the encoding of a byte string and a two-item list is
    not the part of this that anyone should worry about being wrong.
    """
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        if len(item) <= 55:
            return bytes([0x80 + len(item)]) + item
        length = len(item).to_bytes((len(item).bit_length() + 7) // 8, "big")
        return bytes([0xB7 + len(length)]) + length + item
    payload = b"".join(_rlp(x) for x in item)
    if len(payload) <= 55:
        return bytes([0xC0 + len(payload)]) + payload
    length = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
    return bytes([0xF7 + len(length)]) + length + payload


def create_address(deployer: str, nonce: int) -> str:
    """The address ``deployer`` produces at ``nonce`` via ``CREATE``.

    Chain-independent by construction: the derivation has no chain input, which
    is precisely why the same key deploys to matching addresses everywhere.
    """
    if nonce < 0:
        raise ValueError("nonce cannot be negative")
    keccak, checksum = _eth()
    raw = deployer[2:] if deployer.startswith(("0x", "0X")) else deployer
    if len(raw) != 40:
        raise ValueError(f"not a 20-byte address: {deployer!r}")
    # Nonce zero encodes as the empty string, not as a zero byte. Getting this
    # wrong shifts every derived address by one deployment.
    encoded_nonce = b"" if nonce == 0 else nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
    digest = keccak(_rlp([bytes.fromhex(raw), encoded_nonce]))
    return str(checksum(digest[12:]))


def create2_address(deployer: str, salt: str | bytes, init_code_hash: str | bytes) -> str:
    """The address ``CREATE2`` produces.

    Also chain-independent, and also unordered: the salt is chosen, so nothing
    about a CREATE2 address places it in a sequence. Recovering "which
    deployment was this" is not available here the way it is for CREATE.
    """
    keccak, checksum = _eth()

    def _bytes(value: str | bytes, size: int) -> bytes:
        if isinstance(value, bytes):
            raw = value
        else:
            text = value[2:] if value.startswith(("0x", "0X")) else value
            raw = bytes.fromhex(text)
        if len(raw) != size:
            raise ValueError(f"expected {size} bytes, got {len(raw)}")
        return raw

    raw_deployer = deployer[2:] if deployer.startswith(("0x", "0X")) else deployer
    digest = keccak(
        b"\xff" + bytes.fromhex(raw_deployer) + _bytes(salt, 32) + _bytes(init_code_hash, 32)
    )
    return str(checksum(digest[12:]))


def recover_nonce(
    deployer: str, contract: str, *, search: int = DEFAULT_NONCE_SEARCH
) -> int | None:
    """Which nonce of ``deployer`` produced ``contract``, if any within ``search``.

    ``None`` means "not found in this range", never "not deployed by this key".
    The distinction is the whole point: a deployer with four hundred contracts
    would report None for a contract it certainly produced, and reading that as
    a negative is how a real link gets dismissed.
    """
    target = contract.lower()
    for nonce in range(max(0, search)):
        if create_address(deployer, nonce).lower() == target:
            return nonce
    return None


@dataclass(frozen=True, slots=True)
class Deployment:
    """One address a deployer would produce, at a known nonce."""

    nonce: int
    address: str
    is_known: bool = False
    """Whether this address has been seen in the store. False means nobody has
    looked, not that nothing is there --- an unexamined rehearsal contract is
    exactly what this is for finding."""


def deployments_for(
    deployer: str, *, count: int = 10, known: set[str] | None = None
) -> list[Deployment]:
    """The first ``count`` addresses ``deployer`` produces via CREATE.

    Useful in the direction people rarely use it: given an actor's key, this
    enumerates infrastructure that exists whether or not anybody has found it.
    Rehearsal deployments at low nonces are a recurring pattern.
    """
    seen = {a.lower() for a in (known or set())}
    return [
        Deployment(
            nonce=nonce,
            address=(address := create_address(deployer, nonce)),
            is_known=address.lower() in seen,
        )
        for nonce in range(max(0, count))
    ]


def confirm_deployments(
    deployer: str,
    code_at: Any,
    *,
    count: int = 20,
    known: set[str] | None = None,
) -> tuple[list[Deployment], list[str]]:
    """Derive a deployer's CREATE addresses and ask the chain which exist.

    Derivation alone says what an address *would* be; it cannot say whether
    anything is there. This is the second half, and field notes call it the
    universal fallback for a chain with no explorer API --- BSC has no
    Blockscout instance, BscScan wants a key, and this needs neither.

    ``code_at`` is called with an address and returns its code, empty for an
    account that is not a contract. Injected rather than taken from a router so
    the same function works against a live node, a cassette, or a batch of
    ``eth_getCode`` results somebody already collected.

    Returns the confirmed deployments and, separately, the nonces that could
    not be checked. A provider failing on nonce 7 is not evidence that nothing
    was deployed at nonce 7, and folding the two together would report a hole
    in the data as an absence of infrastructure.

    **Ask at a block, not at the tip.** A contract that self-destructed has no
    code now and existed then, so a query against ``latest`` reports an actor's
    infrastructure as never having been there. That is the caller's choice to
    make, which is why ``code_at`` takes the block rather than this function
    fixing one.
    """
    confirmed: list[Deployment] = []
    unchecked: list[str] = []
    for candidate in deployments_for(deployer, count=count, known=known):
        try:
            code = code_at(candidate.address)
        except Exception as exc:
            unchecked.append(f"nonce {candidate.nonce}: {exc}")
            continue
        # Empty, "0x", and "0x0" all mean no contract. A provider returning an
        # error string here would be truthy, which is why the check is for
        # meaningful hex rather than for truthiness.
        body = (code or "").removeprefix("0x").removeprefix("0X")
        if body and body.strip("0"):
            confirmed.append(candidate)
    return confirmed, unchecked


@dataclass
class MultiChainPresence:
    """Where one address appears, and what that does and does not establish."""

    address: str
    chains: list[ChainId] = field(default_factory=list)
    deployer: str | None = None
    nonce: int | None = None
    sibling_deployments: list[Deployment] = field(default_factory=list)

    @property
    def is_deterministic(self) -> bool:
        """Whether a deployer and nonce were recovered for this address."""
        return self.deployer is not None and self.nonce is not None

    def summary(self) -> str:
        chains = ", ".join(str(c) for c in self.chains) or "no chains in this store"
        if not self.is_deterministic:
            return (
                f"{self.address} appears on {chains}. On its own that establishes "
                f"nothing: every address exists on every EVM chain by "
                f"construction, so presence is the default state rather than a "
                f"finding."
            )
        return (
            f"{self.address} appears on {chains}, and is the CREATE output of "
            f"{self.deployer} at nonce {self.nonce}. The matching addresses are "
            f"not the evidence --- derivation guarantees those. The shared "
            f"deployer is. Note that identical addresses can still hold "
            f"different code; confirming they match needs the code compared."
        )

    def attribution(self, chain: ChainId | None = None) -> Attribution | None:
        """A claim about shared control, or ``None`` if there is nothing to claim.

        Capped at MEDIUM. Deriving an address proves who *can* have deployed it
        at that nonce, not that they did anything else --- and a key is not a
        person.
        """
        if not self.is_deterministic:
            return None
        return Attribution(
            label=f"deployed by {self.deployer} at nonce {self.nonce}",
            category=Category.CONTRACT,
            confidence=Confidence.MEDIUM,
            method=Method.ONCHAIN,
            source="chainscope CREATE derivation",
            address=self.address,
            chain=chain,
            rationale=self.summary(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "chains": [str(c) for c in self.chains],
            "deployer": self.deployer,
            "nonce": self.nonce,
            "deterministic": self.is_deterministic,
            "siblings": [
                {"nonce": d.nonce, "address": d.address, "known": d.is_known}
                for d in self.sibling_deployments
            ],
            "summary": self.summary(),
        }


def correlate(
    address: str,
    *,
    chains: list[ChainId] | None = None,
    deployer: str | None = None,
    known_addresses: set[str] | None = None,
    search: int = DEFAULT_NONCE_SEARCH,
    siblings: int = 10,
) -> MultiChainPresence:
    """Assemble what is known about one address across chains.

    ``deployer`` is optional: without it this reports presence and says plainly
    that presence alone establishes nothing. With it, the nonce is recovered and
    the deployer's other output is enumerated --- which is the part worth having.
    """
    presence = MultiChainPresence(address=address, chains=list(chains or []))
    if deployer:
        nonce = recover_nonce(deployer, address, search=search)
        if nonce is not None:
            presence.deployer = deployer
            presence.nonce = nonce
            presence.sibling_deployments = deployments_for(
                deployer, count=siblings, known=known_addresses
            )
    return presence
