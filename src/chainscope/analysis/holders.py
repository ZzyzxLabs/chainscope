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


@dataclass
class Link:
    a: str
    b: str
    kind: LinkKind
    detail: str = ""


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


def group_holders(
    addresses: list[str],
    transfers: list[Any],
    *,
    chain: ChainId | None = None,
    funder_degree: float = DEFAULT_FUNDER_DEGREE,
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

    # First funder of each member, and every direct pair, in one pass.
    first_funder: dict[str, str] = {}
    first_at: dict[str, tuple[int, int]] = {}
    direct: set[tuple[str, str]] = set()

    for row in transfers:
        sender = (getattr(getattr(row, "sender", None), "raw", "") or "").lower()
        recipient = (getattr(getattr(row, "recipient", None), "raw", "") or "").lower()
        if not sender or not recipient:
            continue
        block = getattr(row, "block", None) or 0
        index = getattr(row, "index", 0) or 0

        if sender in wanted and recipient in wanted and sender != recipient:
            direct.add(tuple(sorted((sender, recipient))))  # type: ignore[arg-type]

        if recipient in wanted:
            stamp = (block, index)
            if recipient not in first_at or stamp < first_at[recipient]:
                first_at[recipient] = stamp
                first_funder[recipient] = sender

    # A funder that paid a large share of the list is a service. Named, then
    # dropped --- an exchange hot wallet would otherwise merge the whole set.
    by_funder: dict[str, list[str]] = defaultdict(list)
    for member, funder in first_funder.items():
        by_funder[funder].append(member)
    # An address in the supplied list is never a "service", however many of
    # the others it funded. That is the whole signal: one wallet funding
    # eighteen of twenty is the finding, not noise to be filtered out. The
    # degree test exists for *third parties* --- an exchange hot wallet that
    # paid half the holders because that is what exchanges do.
    services = {
        funder
        for funder, paid in by_funder.items()
        if funder not in wanted and len(paid) > max(2, funder_degree * len(wanted))
    }
    for funder in sorted(services):
        notes.append(
            f"{funder} funded {len(by_funder[funder])} of {len(wanted)} addresses "
            f"and is not itself in the list --- treated as a service, so it was "
            f"not used to link them"
        )
    # Said separately, because it is the opposite conclusion drawn from the
    # same shape. An earlier version reported both as "treated as a service",
    # which was false for the second: those addresses WERE linked by it.
    for funder, paid in sorted(by_funder.items()):
        if funder in wanted and len(paid) > 1:
            notes.append(
                f"{funder} is in the supplied list and funded {len(paid)} of the "
                f"others --- used to link them, and the strongest signal here"
            )

    links: list[Link] = []
    for a, b in sorted(direct):
        links.append(Link(a, b, LinkKind.DIRECT, "value moved between them"))
    for member, funder in sorted(first_funder.items()):
        if funder in wanted and funder != member:
            links.append(
                Link(funder, member, LinkKind.FUNDED_BY_MEMBER, "funded from inside the set")
            )
    for funder, paid in sorted(by_funder.items()):
        if funder in services or funder in wanted or len(paid) < 2:
            continue
        for i, first in enumerate(sorted(paid)):
            for second in sorted(paid)[i + 1 :]:
                links.append(
                    Link(first, second, LinkKind.SHARED_FUNDER, f"both funded by {funder}")
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
