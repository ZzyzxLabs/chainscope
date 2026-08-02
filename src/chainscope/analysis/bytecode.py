"""Is this the same contract, deployed again somewhere else?

The question behind every scam-factory investigation, and the one that turns a
list of addresses into one operator. It has an awkward answer: two deployments
of *identical source* usually have different bytecode.

Solidity appends a CBOR-encoded metadata blob to the end of the runtime code
--- the IPFS hash of the source, the compiler version, and some flags. Change a
comment, compile on a different machine, bump solc by a patch release, and that
tail changes while every instruction before it is byte-identical. Comparing raw
bytecode therefore answers "was this compiled from the same files in the same
place", which is not the question.

The tail is self-describing: its last two bytes are its own length. Read them,
cut that many bytes plus the two, and what remains is the code that runs.

**Raw equality is checked first**, and that is not an optimisation. When two
deployments *are* byte-identical, saying so is a stronger statement than "their
code sections match", and collapsing the two would throw away the difference.
Field notes on a real multi-chain incident make the same point: much of the
time the bytecode is already identical and stripping is unnecessary.

**What a match is and is not.** Identical runtime code means the same
instructions, which is strong evidence of the same author or the same template
--- and no evidence at all about who deployed it. Contract code is public and
copying it is a few keystrokes; a phishing kit resold to twenty operators
produces twenty identical deployments run by twenty different people. The claim
here caps at MEDIUM and says which of the two it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId

__all__ = [
    "BytecodeMatch",
    "compare",
    "group_by_code",
    "strip_metadata",
]

#: Longest metadata blob treated as plausible. Real ones are around 50 bytes;
#: a length header claiming more than this is a malformed or hostile input, and
#: trusting it would cut into the code and make two unrelated contracts match.
MAX_METADATA = 256


def _hexbytes(code: str) -> bytes:
    body = code[2:] if code.startswith(("0x", "0X")) else code
    if len(body) % 2:
        raise ValueError("bytecode has an odd number of hex digits")
    try:
        return bytes.fromhex(body)
    except ValueError as exc:
        raise ValueError(f"not hex: {exc}") from exc


def strip_metadata(code: str) -> bytes:
    """Runtime code with the trailing CBOR metadata removed.

    The last two bytes of a deployed contract are the metadata length, big
    endian. Everything from there back is the blob.

    Returns the input unchanged when the length does not describe a plausible
    blob --- too long, longer than the code itself, or zero. A wrong cut is
    worse than no cut: it removes real instructions and can make two unrelated
    contracts compare equal, which is a false identification rather than a
    missed one.
    """
    raw = _hexbytes(code)
    if len(raw) < 3:
        return raw
    declared = int.from_bytes(raw[-2:], "big")
    if declared == 0 or declared > MAX_METADATA or declared + 2 > len(raw):
        return raw
    return raw[: -(declared + 2)]


@dataclass(frozen=True, slots=True)
class BytecodeMatch:
    """Whether two deployments run the same instructions."""

    identical: bool
    """Byte-for-byte, metadata included. The stronger statement."""

    same_code: bool
    """Equal once metadata is stripped. What "same contract" usually means."""

    stripped_bytes: tuple[int, int]
    """How much was removed from each. Zero means no plausible blob was found,
    which is itself worth seeing --- unverified or hand-written bytecode often
    has none."""

    @property
    def verdict(self) -> str:
        if self.identical:
            return "identical"
        if self.same_code:
            return "same code, different metadata"
        return "different"

    def summary(self) -> str:
        if self.identical:
            return (
                "Byte-for-byte identical, metadata included: compiled from the "
                "same sources with the same compiler and settings."
            )
        if self.same_code:
            return (
                f"The code sections match; only the trailing metadata differs "
                f"({self.stripped_bytes[0]} and {self.stripped_bytes[1]} bytes "
                f"removed). Same instructions, compiled separately --- a "
                f"different machine, a touched comment, or a patch-release "
                f"compiler bump all produce this."
            )
        return "The instructions differ. Not the same contract."

    def attribution(self, address: str, chain: ChainId | None = None) -> Attribution | None:
        """A claim about shared code, or ``None`` when there is nothing to claim."""
        if not self.same_code:
            return None
        return Attribution(
            label="runs identical code to a known contract",
            category=Category.CONTRACT,
            confidence=Confidence.MEDIUM,
            # ONCHAIN: the bytes are on the chain and the comparison is exact.
            # The *inference* it invites is what the cap and rationale limit.
            method=Method.ONCHAIN,
            source="chainscope bytecode comparison",
            address=address,
            chain=chain,
            rationale=(
                f"{self.summary()} That is evidence of a shared author or a "
                f"shared template, and none about who deployed this one: "
                f"contract code is public and copying it takes a moment. A kit "
                f"sold to twenty operators produces twenty identical "
                f"deployments run by twenty different people."
            ),
        )


def compare(left: str, right: str) -> BytecodeMatch:
    """Compare two deployed contracts.

    Raw equality first: when two deployments are byte-identical, saying so is a
    stronger statement than "their code sections match", and collapsing the two
    throws that away.
    """
    raw_left, raw_right = _hexbytes(left), _hexbytes(right)
    # The empty case first. The rule below --- "empty code is not a match with
    # empty code" --- was written and then jumped over by this early return, so
    # `compare("0x", "0x")` came back identical: two addresses that are not
    # contracts reported as running the same thing, which is precisely the
    # false positive the rule exists to prevent.
    if not raw_left or not raw_right:
        return BytecodeMatch(
            identical=False,
            same_code=False,
            stripped_bytes=(0, 0),
        )
    if raw_left == raw_right:
        return BytecodeMatch(identical=True, same_code=True, stripped_bytes=(0, 0))

    code_left, code_right = strip_metadata(left), strip_metadata(right)
    return BytecodeMatch(
        identical=False,
        # Empty code is not a match with empty code. Two addresses that are not
        # contracts would otherwise be reported as running the same thing.
        same_code=bool(code_left) and code_left == code_right,
        stripped_bytes=(len(raw_left) - len(code_left), len(raw_right) - len(code_right)),
    )


def group_by_code(deployments: dict[str, str]) -> dict[bytes, list[str]]:
    """Group addresses by the instructions they run.

    The scam-factory question: given fifty addresses, which are the same
    contract? Keyed by the stripped code so a family survives recompilation.

    Addresses with no code --- an EOA, or a self-destructed contract --- are
    left out rather than grouped together under the empty key, which would
    report every non-contract as one family.
    """
    families: dict[bytes, list[str]] = {}
    for address, code in deployments.items():
        stripped = strip_metadata(code)
        if not stripped:
            continue
        families.setdefault(stripped, []).append(address)
    for members in families.values():
        members.sort()
    return families


def to_dict(match: BytecodeMatch) -> dict[str, Any]:
    return {
        "verdict": match.verdict,
        "identical": match.identical,
        "same_code": match.same_code,
        "metadata_stripped": list(match.stripped_bytes),
        "summary": match.summary(),
    }
