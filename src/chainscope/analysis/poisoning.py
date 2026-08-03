"""Address poisoning: an address generated to be mistaken for another one.

The attack costs nothing and works on attention rather than on cryptography. An
attacker grinds a vanity address matching the first few and last few characters
of one the victim actually transacts with, then sends it a zero-value or dust
transfer so it appears in the victim's history. Later the victim copies "the
address I sent to last time" from that history --- reading, as everybody does,
the first four and last four characters --- and pays the attacker.

**Measured on a real case**: 37 distinct addresses appeared in one subject's
token transfers, and *nine* groups of them shared a 4-character prefix and a
4-character suffix. One group had five members.

That is the finding, and it is a finding precisely because the arithmetic is not
close. Matching 4 hex characters at each end is 32 bits. Across 37 addresses
there are 666 pairs, so the expected number of chance collisions is
666 / 2**32, about 1.6e-7. Nine is not a coincidence that happened to occur; it is
proof of grinding. :func:`chance_of_collision` computes this for whatever set is
actually in front of the reader, because "unlikely" is an adjective and a report
needs the number.

**Which one is real is a separate question, and a harder one.** Two addresses
look alike; the tool must not guess which the victim meant. Three signals
distinguish them, in decreasing strength:

1. The subject *sent value to* it **in a transfer of an asset that can be
   trusted to report honestly**. Poisoning is one-way inbound: an attacker
   cannot make a victim pay them, which is the point of the scam.

   The qualifier is not decoration, and this module was wrong without it. A
   token contract emits its own ``Transfer`` events, so a forged token can log
   any transfer it likes --- including one claiming the victim sent tokens to
   an address of the attacker's choosing. Written without the qualifier, this
   analyzer read the attacker's own logs as evidence about who paid whom and
   reported "the subject paid 4 of these 5", which is both false and the most
   persuasive thing it could have said. Measured on the case above: 24 of the
   27 addresses in a lookalike group appear *only* in forged-token transfers.

   So the trust question is answered first, by
   :mod:`chainscope.analysis.impersonation`, and evidence from an asset that
   fails it counts for nothing here.
2. It appears repeatedly, over time. A poisoning address usually fires once.
3. Its transfers carry value. Zero-value and dust are the attack's signature,
   because the attacker is buying a line in a list, not moving money.

Where these disagree, or where nothing distinguishes the members of a group,
this refuses to nominate one. A confidently-wrong answer here sends somebody's
funds to the attacker, which is the exact harm the module exists to prevent.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..chains import address_key
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context, history_of
from .impersonation import trusted_assets

__all__ = [
    "DEFAULT_EDGES",
    "LookalikeGroup",
    "PoisoningAnalyzer",
    "Sighting",
    "chance_of_collision",
    "find_lookalikes",
    "findings",
]

#: How many characters at each end must match before two addresses are a pair.
#:
#: Four and four. Not a guess: it is what a wallet UI truncates to, and
#: therefore what the victim actually compares. A smaller window would report
#: coincidences --- 2+2 is 16 bits, and 666 pairs of addresses collide there
#: about once every ten sets by chance. A larger one would miss real attacks:
#: grinding 4+4 is cheap, so that is what attackers buy.
DEFAULT_EDGES = 4


@dataclass(frozen=True, slots=True)
class Sighting:
    """What one address did, from the subject's point of view."""

    address: str
    sent_to_by_subject: int = 0
    """Transfers where the subject paid this address, **counting only assets
    that can be trusted to report honestly**. See :attr:`was_paid`."""

    sent_untrusted: int = 0
    """The same, in transfers of an asset that failed the impersonation check.

    Kept and reported rather than discarded, because "the only evidence that
    the subject paid this address comes from a contract the attacker wrote" is
    a fact about the case, and a reader who is not shown it may find the same
    transfer elsewhere and reach the wrong conclusion unaided."""

    received_from: int = 0
    """Transfers where this address paid the subject. Poisoning lives here."""

    zero_value: int = 0
    total_transfers: int = 0
    first_seen: Any = None
    last_seen: Any = None

    @property
    def was_paid(self) -> bool:
        """Whether the subject ever paid this address, *provably*.

        The discriminator that matters, and it only works when the transfer it
        rests on was reported by something other than the attacker. An attacker
        can put an address into somebody's history for free, and --- through a
        token contract they wrote --- can put a *payment* there for free too.
        """
        return self.sent_to_by_subject > 0

    @property
    def only_attacker_authored(self) -> bool:
        """Every transfer involving this address came from an untrusted asset."""
        return (
            self.total_transfers > 0
            and self.sent_to_by_subject == 0
            and (self.sent_untrusted > 0)
        )

    @property
    def only_ever_dust(self) -> bool:
        return self.total_transfers > 0 and self.zero_value == self.total_transfers


@dataclass
class LookalikeGroup:
    """Addresses sharing a prefix and suffix, and what tells them apart."""

    prefix: str
    suffix: str
    members: list[Sighting] = field(default_factory=list)

    @property
    def paid(self) -> list[Sighting]:
        """Members the subject actually paid. Usually one; occasionally none."""
        return [m for m in self.members if m.was_paid]

    @property
    def suspects(self) -> list[Sighting]:
        """Members that only ever appeared inbound.

        Named ``suspects``, not ``impostors``. Being unpaid is not proof: an
        address the subject received a legitimate payment from also sits here.
        """
        return [m for m in self.members if not m.was_paid]

    @property
    def is_decidable(self) -> bool:
        """Whether exactly one member stands out as the one that was meant.

        `False` when the subject paid several of them, or none. Both cases are
        real and both are reported as undecided --- guessing would point at an
        address somebody may then send money to.
        """
        return len(self.paid) == 1 and len(self.suspects) >= 1

    def describe(self) -> str:
        if self.is_decidable:
            real = self.paid[0]
            return (
                f"0x{self.prefix}…{self.suffix}: {len(self.members)} addresses. "
                f"The subject paid {real.address} ({real.sent_to_by_subject} "
                f"transfer(s)); the other {len(self.suspects)} only ever appeared "
                f"inbound, which is what poisoning looks like"
            )
        if not self.paid:
            forged = sum(m.sent_untrusted for m in self.members)
            if forged:
                return (
                    f"0x{self.prefix}…{self.suffix}: {len(self.members)} addresses. "
                    f"{forged} transfer(s) claim the subject paid one of them, and "
                    f"every one of those claims comes from a token contract that "
                    f"failed the impersonation check --- which is to say, from the "
                    f"attacker. Nothing here is evidence of a real payment"
                )
            return (
                f"0x{self.prefix}…{self.suffix}: {len(self.members)} addresses, "
                f"none of which the subject ever paid. Which was intended cannot "
                f"be told from this data --- and may be none of them"
            )
        return (
            f"0x{self.prefix}…{self.suffix}: {len(self.members)} addresses, "
            f"{len(self.paid)} of which the subject paid. Not decidable here: "
            f"naming one would point at an address somebody may then send to"
        )


def chance_of_collision(count: int, edges: int = DEFAULT_EDGES) -> float:
    """Probability that ``count`` unrelated addresses collide at least once.

    The birthday bound. Matching ``edges`` hex characters at each end is
    ``8 * edges`` bits, so with ``C(count, 2)`` pairs the expected number of
    chance collisions is ``pairs / 2**bits`` and the probability of seeing any
    is ``1 - exp(-expected)``.

    This is in the report because "these addresses look similar" invites the
    reply "coincidences happen". They do, at a rate this function states. For
    the 37 addresses in the case that motivated this module the answer is about
    1.6e-7 against nine observed groups, and that number ends the argument in a
    way an adjective cannot.

    Assumes addresses are uniform over the space, which is true of every
    address nobody ground on purpose --- and an address somebody *did* grind is
    the thing being detected, so the assumption failing is the finding.
    """
    if count < 2:
        return 0.0
    pairs = count * (count - 1) / 2
    expected = pairs / (2 ** (8 * edges))
    return float(1 - math.exp(-expected))


def _sightings(transfers: list[Any], subject: str, trusted: set[str]) -> dict[str, Sighting]:
    """Fold a transfer list into one record per counterparty."""
    me = subject.strip().lower()
    sent: dict[str, int] = defaultdict(int)
    sent_bad: dict[str, int] = defaultdict(int)
    received: dict[str, int] = defaultdict(int)
    zero: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    first: dict[str, Any] = {}
    last: dict[str, Any] = {}

    for transfer in transfers:
        sender = _key(getattr(transfer, "sender", None))
        recipient = _key(getattr(transfer, "recipient", None))
        raw = getattr(getattr(transfer, "amount", None), "raw", None)
        when = getattr(transfer, "timestamp", None)
        asset = _key(getattr(transfer, "asset", None))
        believable = asset in trusted
        for other, outbound in ((recipient, True), (sender, False)):
            if not other or other == me:
                continue
            total[other] += 1
            if outbound:
                # The whole correction. A payment logged by a contract the
                # attacker wrote is not a payment; it is a claim by the
                # attacker, and counting it here made the analyzer state the
                # opposite of the truth in the most convincing available words.
                if believable:
                    sent[other] += 1
                else:
                    sent_bad[other] += 1
            else:
                received[other] += 1
            if raw == 0:
                zero[other] += 1
            if when is not None:
                if other not in first or when < first[other]:
                    first[other] = when
                if other not in last or when > last[other]:
                    last[other] = when

    return {
        address: Sighting(
            address=address,
            sent_to_by_subject=sent[address],
            sent_untrusted=sent_bad[address],
            received_from=received[address],
            zero_value=zero[address],
            total_transfers=count,
            first_seen=first.get(address),
            last_seen=last.get(address),
        )
        for address, count in total.items()
    }


def _key(address: Any) -> str:
    if address is None:
        return ""
    raw = getattr(address, "key", None) or getattr(address, "raw", None) or address
    return str(raw).strip().lower()


def find_lookalikes(
    transfers: list[Any],
    subject: str,
    edges: int = DEFAULT_EDGES,
    chain: Any = None,
) -> tuple[list[LookalikeGroup], int]:
    """Group the subject's counterparties by how they *look*.

    Returns ``(groups, addresses_examined)``. The second is needed to state the
    collision probability, and returning it rather than recomputing it upstream
    keeps the number and the set it describes from drifting apart.

    Only groups with more than one member come back. A single address matching
    nothing is the ordinary case and saying so for each of thirty-seven would
    bury the nine that matter.
    """
    seen = _sightings(transfers, subject, trusted_assets(transfers, chain))
    buckets: dict[tuple[str, str], list[Sighting]] = defaultdict(list)
    for address, sighting in seen.items():
        body = address[2:] if address.startswith("0x") else address
        if len(body) < edges * 2:
            # Too short for a prefix and a suffix to be distinct; comparing it
            # would report an address against itself.
            continue
        buckets[(body[:edges], body[-edges:])].append(sighting)

    groups = [
        LookalikeGroup(
            prefix=prefix,
            suffix=suffix,
            # Most-seen first: within a group, the address with a real history
            # is the one somebody meant, and it should not be buried.
            members=sorted(members, key=lambda m: (-m.total_transfers, m.address)),
        )
        for (prefix, suffix), members in buckets.items()
        if len(members) > 1
    ]
    groups.sort(key=lambda g: (-len(g.members), g.prefix))
    return groups, len(seen)


def findings(
    groups: list[LookalikeGroup], examined: int, edges: int = DEFAULT_EDGES
) -> list[Finding]:
    """Turn the groups into findings, leading with the arithmetic."""
    if not groups:
        return []

    probability = chance_of_collision(examined, edges)
    out = [
        Finding(
            title=(
                f"{len(groups)} group(s) of addresses share a {edges}-character "
                f"prefix and suffix"
            ),
            # CRITICAL because the consequence is a payment to the wrong party.
            # This is not a note about the case; it is a warning about an action
            # the reader is about to take.
            severity=Severity.CRITICAL,
            detail=(
                f"Across {examined} counterparties. Matching {edges} hex characters "
                f"at each end is {8 * edges} bits, so the chance that any two of "
                f"{examined} unrelated addresses collide is {probability:.2e}.\n"
                f"\n"
                f"Observing {len(groups)} is therefore not a coincidence --- these "
                f"addresses were generated to be mistaken for each other. Copying "
                f"one from a transaction list, which is where the first and last "
                f"four characters are all anybody reads, is the attack."
            ),
            data={
                "groups": len(groups),
                "addresses_examined": examined,
                "edges": edges,
                "chance_of_one_collision": probability,
            },
        )
    ]

    for group in groups:
        decidable = group.is_decidable
        out.append(
            Finding(
                title=(
                    f"0x{group.prefix}…{group.suffix}: {len(group.members)} "
                    f"addresses that read alike"
                ),
                severity=Severity.IMPORTANT if decidable else Severity.NOTABLE,
                detail=group.describe()
                + "\n\n"
                + "\n".join(
                    f"  - {m.address}\n"
                    f"      paid by subject: {m.sent_to_by_subject}, "
                    f"received from: {m.received_from}, "
                    f"zero-value: {m.zero_value} of {m.total_transfers}"
                    for m in group.members
                )
                + (
                    ""
                    if decidable
                    else "\n\n  No member is nominated. Naming one would point at an "
                    "address\n  somebody may then send money to, and the data here "
                    "does not\n  support it."
                ),
                data={
                    "prefix": group.prefix,
                    "suffix": group.suffix,
                    "decidable": decidable,
                    "paid_by_subject": [m.address for m in group.paid],
                    "suspects": [m.address for m in group.suspects],
                },
            )
        )
    return out


class PoisoningAnalyzer(Analyzer):
    """Which of an address's counterparties were ground to resemble another."""

    name = "poisoning"
    description = "find addresses generated to be mistaken for a real counterparty"
    requires = Capability.ASSET_TRANSFERS

    def applicable(self, ctx: Context) -> bool:
        return bool(ctx.router.candidates(ctx.chain, self.requires))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        edges: int = DEFAULT_EDGES,
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        if not address:
            raise ValueError("poisoning analysis needs an `address` whose history to read")
        started = datetime.now(timezone.utc)
        seed = address_key(ctx.chain, address)
        limit = ctx.limit("per_node", 1000)

        # Both directions, and inbound is the important one: the poisoning
        # transfer is something done *to* the subject, which they never
        # acknowledged and may never have noticed.
        transfers, notes = history_of(
            ctx,
            lambda p: p.asset_transfers(
                ctx.chain,
                seed,
                direction="all",
                start_block=start_block,
                end_block=end_block,
                limit=limit,
            ),
        )
        rows = list(transfers)
        groups, examined = find_lookalikes(rows, seed, edges=edges, chain=ctx.chain)

        warnings = list(notes)
        if len(rows) >= limit:
            warnings.append(
                f"stopped at {limit} transfers (per_node limit). The collision "
                f"probability below is over the {examined} counterparties actually "
                f"read, and a lookalike pair whose second member falls outside that "
                f"window does not appear at all"
            )
        if groups:
            warnings.append(
                f"{len(groups)} group(s) of addresses differ only in the middle. "
                f"Do not copy an address out of a transaction list in this case"
            )
        return Result(
            analyzer=self.name,
            findings=tuple(findings(groups, examined, edges)),
            warnings=tuple(warnings),
            evidence=ctx.evidence(),
            params={"address": address, "edges": edges},
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
