"""Memos: data written onto a chain, and who actually wrote it.

Solana's SPL Memo program attaches arbitrary bytes to a transaction. It is
meant for payment references and it works equally well as a publishing channel
--- a malware campaign can put its command-and-control addresses there, and
infected machines read them with a public RPC call and no infrastructure of
their own to seize.

**The distinction this module exists for.** A memo that appears in an address's
transaction history was not necessarily written by that address. Anyone can
send it 0 lamports with a memo attached, and the memo is then in its history
forever. A recorded campaign did exactly that: one wallet injected memos into
another's feed, and reading the feed as that address's own output attributes
somebody else's instructions to it.

So every memo here carries its **signer**, and "appears in the history of" is a
different query from "was written by". Nothing in this module lets the two be
confused, because the cost of confusing them is naming the wrong operator.

Payloads are decoded permissively and reported honestly: Base64 where it
decodes to text, UTF-8 otherwise, and raw bytes when neither works. A payload
that will not decode is *reported as undecodable*, not skipped --- an operator
who switched encoding mid-campaign leaves exactly that trace, and a decoder
that quietly drops what it cannot read hides the switch.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Memo",
    "MemoFeed",
    "authored_by",
    "decode_payload",
    "extract_indicators",
]

#: Dotted-quad candidates. Range-checked separately --- a regex that accepts
#: 999.999.999.999 finds "IP addresses" in version strings and build numbers.
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")

#: Scheme, host, optional port. Deliberately not a full URL grammar: this is
#: for spotting an endpoint in a payload, not for parsing one to visit.
_URL = re.compile(r"\b(?:https?|wss?|tcp)://[^\s\"'<>\\]{3,}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Memo:
    """One memo, and the signer who put it there."""

    tx: str
    signer: str
    """Who signed the transaction. **The author.** Not the address whose
    history it turns up in, which anyone can add to for the price of a fee."""

    raw: str
    """The payload exactly as it was on chain, before any decoding."""

    slot: int | None = None
    lamports: int | None = None
    """Value moved alongside. Zero is the interesting case: a transfer of
    nothing exists only to carry the memo."""

    @property
    def is_injection(self) -> bool:
        """Whether this looks like a memo posted into somebody else's feed.

        Zero value is the signature. A transfer that moves nothing is not a
        payment with a note attached; the note is the entire point.
        """
        return self.lamports == 0

    def decoded(self) -> tuple[str, str]:
        """The payload and how it was read: ``base64``, ``utf-8``, or ``raw``."""
        return decode_payload(self.raw)

    def to_dict(self) -> dict[str, Any]:
        text, encoding = self.decoded()
        return {
            "tx": self.tx,
            "signer": self.signer,
            "slot": self.slot,
            "lamports": self.lamports,
            "zero_value": self.is_injection,
            "encoding": encoding,
            "text": text,
            "indicators": extract_indicators(text),
        }


def decode_payload(raw: str) -> tuple[str, str]:
    """Read a memo payload, and say how it was read.

    Base64 first when it round-trips to printable text, then the raw string.
    The encoding label travels with the result because "this decoded from
    Base64" and "this was already text" are different observations about an
    operator, and a campaign that switches mid-run is visible only if both are
    recorded.

    Never raises. A payload that decodes to nothing readable comes back with
    the ``raw`` label rather than being dropped --- an unreadable memo is a
    fact about the feed, and a decoder that silently skips what it cannot read
    hides the moment the format changed.
    """
    text = raw.strip()
    if not text:
        return "", "raw"

    # Base64 must round-trip *and* produce text. Plenty of ordinary strings
    # decode to bytes; only a real payload decodes to something printable.
    if len(text) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text):
        try:
            candidate = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
        else:
            if candidate.isprintable() or "\n" in candidate:
                return candidate, "base64"

    return raw, "utf-8" if raw.isprintable() else "raw"


def extract_indicators(text: str) -> dict[str, list[str]]:
    """Network indicators in a decoded payload.

    Extracted, never resolved and never contacted. A tool that reached out to
    confirm an address would announce the investigation to the operator, and
    the challenge notes make the same point in plainer words: do not connect to
    the host.

    JSON payloads are also read structurally, because a campaign publishing a
    config object puts the port in a field rather than in the URL --- and a
    regex over the serialised form finds the host and loses the port.
    """
    found: dict[str, list[str]] = {"ipv4": [], "urls": [], "ports": []}

    for match in _IPV4.finditer(text):
        octets = [int(g) for g in match.groups()]
        # Range-checked: a bare regex reports 999.1.1.1 and finds addresses in
        # version strings.
        if all(0 <= o <= 255 for o in octets):
            found["ipv4"].append(match.group(0))

    found["urls"] = _URL.findall(text)

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if "port" in key.lower() and isinstance(value, (int, str)):
                found["ports"].append(str(value))
            elif isinstance(value, str):
                for match in _IPV4.finditer(value):
                    if all(0 <= int(g) <= 255 for g in match.groups()):
                        found["ipv4"].append(match.group(0))

    # Deduplicated, order preserved: the *first* appearance is usually the one
    # that matters, and sorting would lose it.
    return {k: list(dict.fromkeys(v)) for k, v in found.items()}


@dataclass
class MemoFeed:
    """Every memo in one address's history, split by who wrote it."""

    address: str
    own: list[Memo] = field(default_factory=list)
    injected: list[Memo] = field(default_factory=list)
    """Signed by somebody else. Present in this address's history and *not* its
    output --- reading these as the address's own attributes another party's
    instructions to it."""

    @property
    def injectors(self) -> list[str]:
        return sorted({m.signer for m in self.injected})

    def summary(self) -> str:
        parts = [f"{len(self.own)} memo(s) signed by {self.address}"]
        if self.injected:
            parts.append(
                f"{len(self.injected)} signed by {len(self.injectors)} other "
                f"address(es) and posted into this feed --- anyone can do that "
                f"for the price of a fee, and reading them as this address's "
                f"own output attributes somebody else's instructions to it"
            )
        return "; ".join(parts) + "."


def authored_by(memos: list[Memo], address: str) -> MemoFeed:
    """Split a feed into what this address wrote and what was written at it.

    Compared **exactly**, and the address is kept as given. This module is
    about SPL memos, and a Solana address is base58 --- where case is part of
    the value, not presentation. `7xKX` and `7xkx` are different accounts, so
    lowercasing before comparing both invents matches between unrelated
    accounts and, because base58 excludes some characters, can fold two real
    addresses onto one key. The EVM habit of case-insensitive comparison is
    correct for hex and wrong everywhere else.
    """
    feed = MemoFeed(address=address)
    for memo in memos:
        if memo.signer == address:
            feed.own.append(memo)
        else:
            feed.injected.append(memo)
    return feed
