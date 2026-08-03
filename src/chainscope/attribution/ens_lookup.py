"""Actually asking the chain what an ENS name says.

:mod:`chainscope.attribution.ens` has the whole of the hard part --- namehash,
the reverse node, and the forward-confirmation rule that is the only thing
standing between "this address has a name" and "anybody can point a name at any
address". :mod:`chainscope.osint.leads` turns a confirmed record's text entries
into leads. :mod:`chainscope.case.leads` stores them.

None of it ran, because nothing constructed an :class:`~.ens.EnsRecord`. Three
carefully-written modules in a chain with no first link. §2 of `docs/needs.md`
again, and this is the link.

**The lookup is three calls and the order matters.**

1. ``name(reverse_node(address))`` on the reverse resolver --- what the address
   claims to be called.
2. ``addr(namehash(name))`` --- where that name actually points.
3. Only if (2) comes back to the address we started from, ``text(node, key)``
   for the keys worth following.

Step 3 is gated on step 2 and that gate is the entire point. An unconfirmed
reverse record is a claim by whoever owns the name, about somebody else's
address --- so its text records are *that stranger's* handles, and filing them
against this address would attach another person's identity to it. Worse than
finding nothing, and it looks the same in a report.

**A resolver of zero is not a resolver.** The registry returns the zero address
for a name nobody has configured, and the ABI decoder is perfectly happy to
decode a call to it into an empty string. That empty string is indistinguishable
from "this name has no Twitter" unless the zero check happens first, which is
why it happens first and why this docstring says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.chainid import ETHEREUM, ChainId
from ..osint.leads import TEXT_KEYS, Lead, leads_from_text_records
from .ens import EnsRecord, namehash, normalise_name, reverse_node

__all__ = ["ENS_REGISTRY", "EnsLookup", "LookupResult"]

#: The ENS registry, at the same address on every network that has one.
#:
#: Hard-coded rather than discovered. It is an immutable contract deployed in
#: 2019 and its address is part of the protocol; looking it up would add a call
#: and a failure mode to obtain a constant.
ENS_REGISTRY = "0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e"

#: Function selectors, first four bytes of the keccak of each signature.
#:
#: Written out rather than computed at import, so that a wrong one is a wrong
#: constant somebody can check against the ABI rather than a hashing bug three
#: layers down. `tests/unit/test_ens_lookup.py` recomputes each from its
#: signature and fails if they disagree.
_RESOLVER = "0178b8bf"  # resolver(bytes32)
_NAME = "691f3431"  # name(bytes32)
_ADDR = "3b3b57de"  # addr(bytes32)
_TEXT = "59d1d43c"  # text(bytes32,string)

_ZERO = "0x0000000000000000000000000000000000000000"


@dataclass
class LookupResult:
    """What one address's ENS entry says, and what may be done with it."""

    record: EnsRecord
    text: dict[str, str]
    leads: list[Lead]
    notes: list[str]

    @property
    def confirmed(self) -> bool:
        return self.record.is_confirmed


class EnsLookup:
    """Reads ENS through any provider that can make an ``eth_call``."""

    def __init__(self, provider: Any, chain: ChainId = ETHEREUM) -> None:
        self.provider = provider
        self.chain = chain

    # -- the three calls -------------------------------------------------

    def _call(self, to: str, data: str) -> str:
        return str(self.provider.call(self.chain, to, data) or "0x")

    def resolver_for(self, node: bytes) -> str:
        """The resolver contract for a node, or ``""`` if none is set.

        The empty string, not the zero address. A caller that forgets to check
        would otherwise `eth_call` into address zero, which returns empty data
        that decodes cleanly to an empty string --- and "no resolver" would
        become "no Twitter handle", which is a different and unfalsifiable
        claim.
        """
        raw = self._call(ENS_REGISTRY, "0x" + _RESOLVER + node.hex())
        address = _decode_address(raw)
        return "" if address in ("", _ZERO) else address

    def name_of(self, address: str) -> str:
        """What the address says it is called. Unverified by construction."""
        node = reverse_node(address)
        resolver = self.resolver_for(node)
        if not resolver:
            return ""
        return _decode_string(self._call(resolver, "0x" + _NAME + node.hex()))

    def address_of(self, name: str) -> str | None:
        """Where a name points, or ``None`` if it has no resolver.

        ``None`` and ``""`` differ and :class:`~.ens.EnsRecord` treats them
        differently: ``None`` means the forward direction was never checked, so
        no claim can be made either way, while ``""`` means it was checked and
        resolves nowhere. Collapsing them would let "we did not look" be read as
        "it does not confirm".
        """
        node = namehash(normalise_name(name))
        resolver = self.resolver_for(node)
        if not resolver:
            return None
        return _decode_address(self._call(resolver, "0x" + _ADDR + node.hex()))

    def text_of(self, name: str, keys: tuple[str, ...] = ()) -> dict[str, str]:
        """Text records for a name, for the keys worth following.

        Only the known keys, never "everything the resolver holds". A resolver
        can carry arbitrary keys and turning an unrecognised one into a lead
        named after itself produces confident-looking noise --- in the module
        whose material is already the least reliable thing here.
        """
        node = namehash(normalise_name(name))
        resolver = self.resolver_for(node)
        if not resolver:
            return {}
        found: dict[str, str] = {}
        for key in keys or tuple(TEXT_KEYS):
            data = "0x" + _TEXT + node.hex() + _encode_string_arg(key)
            value = _decode_string(self._call(resolver, data))
            if value.strip():
                found[key] = value.strip()
        return found

    # -- the whole pass --------------------------------------------------

    def look_up(self, address: str) -> LookupResult:
        """Resolve, confirm, and turn what survives into leads.

        Text records are fetched **only** after forward-confirmation succeeds.
        Fetching them first and filtering later would be one round trip cheaper
        and would put another person's handles in the cache under this
        address's key, where something downstream will eventually read them.
        """
        notes: list[str] = []
        name = self.name_of(address)
        if not name:
            return LookupResult(
                record=EnsRecord(address=address),
                text={},
                leads=[],
                notes=[
                    "no reverse ENS record. Most addresses have none; this says "
                    "nothing about who controls it"
                ],
            )

        forward = self.address_of(name)
        record = EnsRecord(address=address, name=name, forward_address=forward)

        if not record.was_checked:
            notes.append(
                f"{name} has no resolver, so the forward direction could not be "
                f"checked. Unconfirmed is not the same as refuted, and neither "
                f"supports a claim"
            )
            return LookupResult(record=record, text={}, leads=[], notes=notes)

        if not record.is_confirmed:
            notes.append(
                f"{name} does not resolve back to {address} --- it points at "
                f"{forward or 'nothing'}. Anybody may point a name at any "
                f"address, so this is a claim by whoever owns {name}, about "
                f"somebody else. Its text records are theirs, not this "
                f"address's, and are not read"
            )
            return LookupResult(record=record, text={}, leads=[], notes=notes)

        text = self.text_of(name)
        leads = leads_from_text_records(record, text)
        if not leads and text:
            notes.append(
                f"{len(text)} text record(s) present, none under a key this "
                f"package recognises. Unknown keys are skipped rather than "
                f"passed through: a lead named after a key nobody understands "
                f"reads as a finding about a field somebody vetted"
            )
        return LookupResult(record=record, text=text, leads=leads, notes=notes)


# -- minimal ABI decoding ------------------------------------------------
#
# Hand-written rather than pulled from `eth-abi`. Three types are needed ---
# address, string, and a string argument --- and the dependency is an optional
# extra this package does not otherwise require at this layer. Each function
# states what it does with malformed input, because a decoder that returns a
# plausible empty value on garbage is how "no Twitter handle" gets manufactured.


def _decode_address(raw: str) -> str:
    """The last 20 bytes of a 32-byte word, or ``""`` if it is not one."""
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) < 64:
        return ""
    return "0x" + body[24:64]


def _decode_string(raw: str) -> str:
    """A solidity ``string`` return, or ``""`` when it is not decodable.

    Empty on anything unexpected --- short data, a length past the end of the
    buffer, bytes that are not UTF-8. The alternative is raising, and a resolver
    returning junk for one text key should not abandon the other six; the
    absence is visible either way because the key simply does not appear.
    """
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) < 128:
        return ""
    try:
        offset = int(body[:64], 16) * 2
        length = int(body[offset : offset + 64], 16) * 2
        if length == 0 or offset + 64 + length > len(body):
            return ""
        return bytes.fromhex(body[offset + 64 : offset + 64 + length]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _encode_string_arg(value: str) -> str:
    """A ``string`` as the second argument of a two-argument call.

    Offset 0x40 because the first argument is the 32-byte node, so the dynamic
    data begins after two words.
    """
    data = value.encode("utf-8")
    padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return f"{64:064x}" + f"{len(data):064x}" + padded.hex()
