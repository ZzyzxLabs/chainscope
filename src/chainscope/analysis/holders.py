"""Group a list of addresses into the parties that appear to control them.

The question this answers is not "are these two related" but "how many people
are behind these forty addresses, and what does the biggest one actually
hold". That is the shape of the work when a token's holder list is public and
the concentration is not: twenty wallets each holding 4% look like a healthy
distribution, and if one person funded eighteen of them it is a 76% position.

**Every link is typed and every group carries the links that formed it.** The
grouping is a connected-components pass, so one weak edge can drag two real
parties together --- and if the reason were not attached, the merged group
would look exactly like a strong one. `LinkKind` below is not a score: a
direct transfer and a shared funder are different kinds of evidence, and
averaging them would produce a number nobody could act on.

**A shared funder is the weakest edge here and the easiest to over-read.** An
exchange pays out to thousands of unrelated people from one hot wallet, which
is why `funder_degree` exists: a funder that paid a large share of the whole
list is a service, not a person, and its edges are dropped with that stated in
the result rather than silently.

**Ungrouped means unlinked *by these signals*, not independent.** An address
alone in its group has no observed link in what was fetched. That is the same
distinction the rest of this package draws everywhere, and it matters most
here, because the headline number people want is "the top holder controls
X%" and every missed link makes X too small.

**Measured and rejected: gas-price fingerprinting.** The idea is that people
set a characteristic tip and can be recognised by it. Sampled over four blocks
on two chains: Ethereum, 1,720 transactions from 1,564 addresses, 4.74 bits of
entropy, one tip value (0.005 gwei) carrying 30.2% of them; BSC, 205
transactions from 171 addresses, 4.13 bits, one value carrying 33.2%. Linking
every pair that shares a tip joins **15.5% of all possible pairs, on both
chains independently** --- because since EIP-1559 the tip is computed by the
wallet, so the buckets are wallet defaults rather than habits. That is not a
weak signal to be used carefully; it is a link generator with the shape of
evidence, which is the failure the typed `LinkKind` above exists to prevent.
Nonce cadence measures the same thing more indirectly and was not re-tested.

What discriminates instead is co-occurrence that is rare *by construction*:
two addresses holding the same obscure token, or buying in the same block at a
pool's launch. There the population sharing the trait is small because almost
nobody touches that token --- not large because every wallet picked the same
default.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from ..core.chainid import ChainId
from ..core.hypothesis import Hypothesis, ScoreFactor
from ..core.result import Finding, Result, Severity
from .base import Analyzer, Context

__all__ = ["DEFAULT_FUNDER_DEGREE", "Group", "HoldersAnalyzer", "LinkKind", "group_holders"]

#: A funder paying more than this share of the supplied list is treated as a
#: service and its edges are dropped. One exchange hot wallet funding half a
#: token's holders is the normal case, not a syndicate.
DEFAULT_FUNDER_DEGREE = 0.25

#: How far back to walk the funding chain. Three, because the shape this is
#: built for is "split, layer, layer, then out to an exchange" --- and because
#: every extra hop widens the set of addresses that can be joined by
#: coincidence. Deeper is available; unlimited is not, by design.
DEFAULT_MAX_DEPTH = 3

#: How many distinct senders an address may have and still look like an
#: exchange deposit address. Kept small: these are issued per customer, and a
#: generous limit turns any quiet wallet into one.
DEFAULT_DEPOSIT_SENDERS = 5

#: Blocks within which two fundings from one address count as the same batch.
#: Twenty-five, about five minutes on Ethereum. It was 100, and at that width
#: six unrelated exchange withdrawals ninety blocks apart were merged into one
#: party --- the over-merge this whole module is arranged against. Somebody
#: splitting a position does it in consecutive transactions, not over an hour.
DEFAULT_BATCH_BLOCKS = 25

#: How close two amounts must be to look like one split rather than two
#: unrelated payments. 0.9, not 0.5: at a half the six withdrawals above
#: qualified, because consecutive multiples of 0.1 ETH are within a half of
#: each other. A deliberate split is near-identical, not merely similar.
DEFAULT_BATCH_RATIO = 0.9


class LinkKind(str, Enum):
    """Why two addresses were joined. Kinds, not degrees --- see the module docstring."""

    DIRECT = "direct_transfer"
    """One sent value to the other. The strongest signal available on an
    account-model chain: it proves contact, though not common ownership."""

    SHARED_FUNDER = "shared_funder"
    """Both were first funded by the same address. Weak on its own --- this is
    what an exchange withdrawal looks like."""

    FUNDED_BY_MEMBER = "funded_by_member"
    """One was funded by another address in the list. Stronger than a shared
    third-party funder: the money came from inside the set being examined."""

    COMMON_ANCESTOR = "common_ancestor"
    """Both trace back to one address through intermediaries. The layered
    version of a shared funder --- three hops to a common origin is the usual
    shape when somebody splits a position deliberately, and it is also what
    two strangers who both once used the same bridge look like. Depth and the
    path travelled are carried so the difference stays visible."""

    SHARED_DEPOSIT = "shared_deposit"
    """Both paid into the same exchange deposit address.

    The strongest signal in this module, and the one that most needs its
    direction kept straight. An exchange **hot wallet** pays out to millions
    and links nobody. An exchange **deposit address** is assigned to one
    customer, takes money in from that customer, and forwards it to the
    exchange --- so two addresses paying into the same one are the same
    account holder, or somebody depositing on their behalf.

    Identified by shape rather than by a label: few distinct senders, and its
    own onward funding goes to an address that pays a great many others."""

    BATCH_FUNDED = "batch_funded"
    """Funded by one address, close together in time, in similar amounts.
    A person splitting a position does it in one sitting; an exchange pays
    its customers at random times in random amounts, so this separates the
    two where a bare shared funder cannot."""


@dataclass
class Link:
    a: str
    b: str
    kind: LinkKind
    detail: str = ""
    depth: int = 1
    """Hops between the two. One is a direct relation; three is a chain of
    intermediaries and must not be displayed as if it were the same claim."""

    through: tuple[str, ...] = ()
    """The intermediaries walked, so a reader can check them."""


@dataclass
class Group:
    """One apparent party, and the evidence that assembled it."""

    members: list[str]
    links: list[Link] = field(default_factory=list)
    holdings_raw: int = 0

    @property
    def alone(self) -> bool:
        """No observed link. Not the same as independent."""
        return len(self.members) == 1

    def why(self) -> str:
        if self.alone:
            return "no link observed in what was fetched --- not evidence of independence"
        kinds = sorted({link.kind.value for link in self.links})
        return f"{len(self.members)} addresses joined by: {', '.join(kinds)}"


def _funding_graph(transfers: list[Any]) -> tuple[dict[str, str], dict[str, int]]:
    """Who first funded each address, and how many each address funded.

    The out-degree is what identifies a service. It is counted over the whole
    fetched history rather than over the supplied list, because an exchange
    that appears twice in your list has still paid ten thousand people, and
    the list is not evidence about that.
    """
    first: dict[str, str] = {}
    at: dict[str, tuple[int, int]] = {}
    degree: dict[str, int] = defaultdict(int)
    for row in transfers:
        sender = (getattr(getattr(row, "sender", None), "raw", "") or "").lower()
        recipient = (getattr(getattr(row, "recipient", None), "raw", "") or "").lower()
        if not sender or not recipient:
            continue
        stamp = (getattr(row, "block", None) or 0, getattr(row, "index", 0) or 0)
        if recipient not in at or stamp < at[recipient]:
            at[recipient] = stamp
            first[recipient] = sender
    for funded in first.values():
        degree[funded] += 1
    return first, dict(degree)


def _deposit_addresses(
    transfers: list[Any], services: set[str], max_senders: int
) -> dict[str, set[str]]:
    """Exchange deposit addresses, and who paid into each.

    Found by shape, not by a label, because a label for every exchange's
    deposit addresses does not exist --- there are hundreds of millions and
    they are generated per customer.

    The shape is: money arrives from a small number of distinct senders, and
    the address forwards onward to something that pays a great many others.
    That second half is what separates a deposit address from any ordinary
    wallet with few counterparties, and it is why `services` has to be worked
    out first.

    Direction is the entire point. A hot wallet has this shape reversed --- it
    pays many and receives from few --- and treating the two alike would join
    everyone who ever withdrew from an exchange into one party.
    """
    senders: dict[str, set[str]] = defaultdict(set)
    sends_to: dict[str, set[str]] = defaultdict(set)
    for row in transfers:
        sender = (getattr(getattr(row, "sender", None), "raw", "") or "").lower()
        recipient = (getattr(getattr(row, "recipient", None), "raw", "") or "").lower()
        if not sender or not recipient:
            continue
        senders[recipient].add(sender)
        sends_to[sender].add(recipient)

    deposits: dict[str, set[str]] = {}
    for candidate, paid_by in senders.items():
        if candidate in services or not 0 < len(paid_by) <= max_senders:
            continue
        onward = sends_to.get(candidate, set())
        if onward and onward <= services:
            deposits[candidate] = paid_by
    return deposits


def _ancestry(
    address: str,
    first: dict[str, str],
    services: set[str],
    max_depth: int,
) -> list[tuple[str, int, tuple[str, ...]]]:
    """Walk back up the funding chain. Stops at a service, and says so.

    An exchange hot wallet is a boundary, not a link. Three hops from almost
    any address reaches one, and traversing *through* it would join everybody
    who ever withdrew from that exchange into a single party --- a result that
    looks like a whale and is an artefact of not stopping.

    Returns each ancestor with the depth at which it was reached and the path
    walked, so a three-hop relation can be told from a direct one.
    """
    out: list[tuple[str, int, tuple[str, ...]]] = []
    seen = {address}
    at = address
    path: list[str] = []
    for depth in range(1, max_depth + 1):
        parent = first.get(at)
        if not parent or parent in seen:
            break
        if parent in services:
            # Recorded as the boundary it is, not walked through.
            out.append((parent, depth, tuple(path)))
            break
        seen.add(parent)
        out.append((parent, depth, tuple(path)))
        path.append(parent)
        at = parent
    return out


def group_holders(
    addresses: list[str],
    transfers: list[Any],
    *,
    chain: ChainId | None = None,
    funder_degree: float = DEFAULT_FUNDER_DEGREE,
    max_depth: int = DEFAULT_MAX_DEPTH,
    batch_blocks: int = DEFAULT_BATCH_BLOCKS,
    batch_ratio: float = DEFAULT_BATCH_RATIO,
    max_senders: int = DEFAULT_DEPOSIT_SENDERS,
) -> tuple[list[Group], list[str]]:
    """Group ``addresses`` by observed linkage. Returns ``(groups, notes)``.

    ``transfers`` is whatever history has been fetched for the set. Nothing is
    fetched here: an address whose history is absent contributes no edges and
    lands in its own group, which the caller must not read as independence.
    """
    wanted = {a.lower() for a in addresses}
    notes: list[str] = []
    if not wanted:
        return [], ["no addresses supplied"]

    first_funder, out_degree = _funding_graph(transfers)

    # A service is identified by how many addresses it funded across the whole
    # fetched history, and an address in the supplied list is never one: a
    # wallet that funded eighteen of your twenty IS the finding.
    threshold = max(3, int(funder_degree * len(wanted)))
    services = {
        who for who, degree in out_degree.items() if who not in wanted and degree >= threshold
    }

    direct: set[tuple[str, str]] = set()
    batch: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for row in transfers:
        sender = (getattr(getattr(row, "sender", None), "raw", "") or "").lower()
        recipient = (getattr(getattr(row, "recipient", None), "raw", "") or "").lower()
        if not sender or not recipient:
            continue
        if sender in wanted and recipient in wanted and sender != recipient:
            direct.add(tuple(sorted((sender, recipient))))  # type: ignore[arg-type]
        if recipient in wanted and first_funder.get(recipient) == sender:
            amount = int(getattr(getattr(row, "amount", None), "raw", 0) or 0)
            batch[sender].append((recipient, getattr(row, "block", None) or 0, amount))

    links: list[Link] = []
    for a, b in sorted(direct):
        links.append(Link(a, b, LinkKind.DIRECT, "value moved between them", depth=1))

    # Ancestors, walked per member and stopped at services.
    ancestors: dict[str, list[tuple[str, int, tuple[str, ...]]]] = {}
    stopped_at: set[str] = set()
    for member in sorted(wanted):
        chain_up = _ancestry(member, first_funder, services, max_depth)
        ancestors[member] = chain_up
        for who, _, _ in chain_up:
            if who in services:
                stopped_at.add(who)

    for who in sorted(stopped_at):
        notes.append(
            f"{who} funded {out_degree.get(who, 0)} addresses and is not in the "
            f"list --- treated as a service and used as a boundary: the walk "
            f"stopped there rather than joining everyone downstream of it"
        )
    # Every third-party funder that was dropped, not only those a walk reached.
    # A service nobody happened to walk into still shaped the result by not
    # linking anyone, and a reader comparing two runs needs to see it.
    for who in sorted(services - stopped_at):
        if any(first_funder.get(m) == who for m in wanted):
            notes.append(
                f"{who} funded {out_degree.get(who, 0)} addresses and is not in "
                f"the list --- treated as a service, so it was not used to link them"
            )
    # And the opposite conclusion, drawn from the same shape. An address in the
    # supplied list that funded several others is the finding, not noise: an
    # earlier version called it a service and reported it as "not used to link
    # them" while linking them anyway.
    inside: dict[str, int] = defaultdict(int)
    for member in wanted:
        funder = first_funder.get(member)
        if funder in wanted and funder != member:
            inside[funder] += 1
    for who, count in sorted(inside.items()):
        if count > 1:
            notes.append(
                f"{who} is in the supplied list and funded {count} of the others "
                f"--- used to link them, and the strongest signal here"
            )

    # An ancestor inside the list links directly; one outside links the
    # members that share it, at the deeper of the two depths.
    shared: dict[str, list[tuple[str, int, tuple[str, ...]]]] = defaultdict(list)
    for member, chain_up in ancestors.items():
        for who, depth, path in chain_up:
            if who in services:
                continue
            if who in wanted and who != member:
                links.append(
                    Link(
                        who,
                        member,
                        LinkKind.FUNDED_BY_MEMBER if depth == 1 else LinkKind.COMMON_ANCESTOR,
                        f"funded from inside the set, {depth} hop(s) up",
                        depth=depth,
                        through=path,
                    )
                )
            elif who not in wanted:
                shared[who].append((member, depth, path))

    for who, reached in sorted(shared.items()):
        if len(reached) < 2:
            continue
        for i, (m1, d1, p1) in enumerate(sorted(reached)):
            for m2, d2, p2 in sorted(reached)[i + 1 :]:
                deep = max(d1, d2)
                links.append(
                    Link(
                        m1,
                        m2,
                        LinkKind.COMMON_ANCESTOR if deep > 1 else LinkKind.SHARED_FUNDER,
                        f"both trace to {who} within {deep} hop(s)",
                        depth=deep,
                        through=tuple(sorted(set(p1) | set(p2))),
                    )
                )

    # Two members paying into one deposit address are one customer. Placed
    # before the batch test because it is the stronger claim of the two.
    for deposit, paid_by in sorted(
        _deposit_addresses(transfers, services, max_senders).items()
    ):
        members_here = sorted(paid_by & wanted)
        if len(members_here) < 2:
            continue
        for i, m1 in enumerate(members_here):
            for m2 in members_here[i + 1 :]:
                links.append(
                    Link(
                        m1,
                        m2,
                        LinkKind.SHARED_DEPOSIT,
                        f"both paid into {deposit}, which forwards to a service "
                        f"and takes money from {len(paid_by)} address(es)",
                        depth=1,
                    )
                )

    # Same funder, close in time, similar amounts: a person splitting a
    # position does it in one sitting. An exchange pays at random times in
    # random amounts, which is what makes this separable from a bare shared
    # funder.
    for funder, paid in sorted(batch.items()):
        if len(paid) < 2:
            continue
        paid.sort(key=lambda x: x[1])
        for i, (m1, b1, a1) in enumerate(paid):
            for m2, b2, a2 in paid[i + 1 :]:
                if b2 - b1 > batch_blocks:
                    break
                # An unknown amount cannot satisfy a similarity test. Written
                # as `a1 and a2 and ratio < r`, a missing amount skipped the
                # check and linked the pair --- "we do not know" behaving as
                # "close enough", which is the failure this package exists to
                # refuse, arriving in a boolean.
                if not (a1 and a2):
                    continue
                if min(a1, a2) / max(a1, a2) < batch_ratio:
                    continue
                links.append(
                    Link(
                        m1,
                        m2,
                        LinkKind.BATCH_FUNDED,
                        f"funded by {funder} within {b2 - b1} block(s), similar amounts",
                        depth=1,
                    )
                )

    # Connected components over the links.
    parent = {a: a for a in wanted}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in links:
        ra, rb = find(link.a), find(link.b)
        if ra != rb:
            parent[ra] = rb

    members: dict[str, list[str]] = defaultdict(list)
    for a in sorted(wanted):
        members[find(a)].append(a)

    groups = []
    for ms in members.values():
        held = {tuple(sorted((link.a, link.b))) for link in links}
        mine = [
            link
            for link in links
            if link.a in ms and link.b in ms and tuple(sorted((link.a, link.b))) in held
        ]
        groups.append(Group(members=sorted(ms), links=mine))
    groups.sort(key=lambda g: (-len(g.members), g.members[0]))
    return groups, notes


class HoldersAnalyzer(Analyzer):
    """Group a supplied holder list into apparent parties."""

    name = "linked_holders"
    version = "1.0"
    description = "Group a list of addresses into the parties that appear to control them"

    #: See `Analyzer.REQUIRES`. The list is the whole input; there is no
    #: sensible default and running against one address answers nothing.
    REQUIRES: ClassVar[tuple[str, ...]] = ("addresses",)

    def run(self, ctx: Context, *, addresses: str = "", rows: Any = None, **_: Any) -> Result:
        wanted = [a.strip() for a in addresses.split(",") if a.strip()]
        if len(wanted) < 2:
            raise ValueError(
                "linked_holders needs `addresses`: two or more comma-separated "
                "addresses. Grouping one address answers nothing"
            )

        groups, notes = group_holders(wanted, list(rows or ()), chain=ctx.chain)
        linked = [g for g in groups if not g.alone]
        alone = [g for g in groups if g.alone]

        findings = [
            Finding(
                title=f"{len(groups)} apparent parties behind {len(wanted)} addresses",
                detail=(
                    f"{len(linked)} group(s) hold more than one address; "
                    f"{len(alone)} address(es) showed no link. An address alone "
                    f"here is unlinked by these signals, which is not the same "
                    f"as independent --- if its history was never fetched it "
                    f"could not have been linked."
                ),
                severity=Severity.INFO,
                data={"groups": len(groups), "supplied": len(wanted)},
            )
        ]
        for group in linked:
            findings.append(
                Finding(
                    title=f"{len(group.members)} addresses appear to be one party",
                    detail=group.why()
                    + "; "
                    + "; ".join(
                        f"{link.a[:10]}…/{link.b[:10]}…: {link.detail}"
                        for link in group.links[:4]
                    ),
                    severity=Severity.INFO,
                    data={"members": group.members},
                )
            )

        hypotheses = []
        biggest = linked[0] if linked else None
        if biggest:
            share = len(biggest.members) / len(wanted)
            hypotheses.append(
                Hypothesis(
                    claim=(
                        f"one party controls {len(biggest.members)} of the "
                        f"{len(wanted)} addresses supplied"
                    ),
                    factors=(
                        ScoreFactor(
                            name="share of the list in the largest group",
                            weight=share,
                            value=f"{len(biggest.members)}/{len(wanted)}",
                            note="a bigger share is a stronger claim about concentration",
                        ),
                        ScoreFactor(
                            name="strongest link kind present",
                            weight=1.0
                            if any(link.kind is LinkKind.DIRECT for link in biggest.links)
                            else 0.5,
                            value=", ".join(
                                sorted({link.kind.value for link in biggest.links})
                            ),
                            note=(
                                "a direct transfer proves contact; a shared funder "
                                "is what an exchange withdrawal also looks like"
                            ),
                        ),
                    ),
                    data={"members": biggest.members},
                )
            )

        return Result(
            analyzer=self.name,
            findings=tuple(findings),
            hypotheses=tuple(hypotheses),
            warnings=tuple(notes),
        )
