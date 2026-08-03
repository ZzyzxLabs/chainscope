"""Grouping a holder list into the parties behind it.

The question is not "are these two related" but "how many people hold this
token, and what does the biggest one actually control". Twenty wallets at 4%
each look like a healthy distribution; if one address funded eighteen of them
it is a 76% position.

The failure that matters is over-merging. One exchange hot wallet pays out to
thousands of unrelated people, and treating that as a link would collapse the
whole list into one imaginary whale --- which reads as a finding rather than as
the artefact it is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from chainscope.analysis.holders import Group, LinkKind, group_holders


def transfer(sender: str, recipient: str, block: int, index: int = 0):
    return SimpleNamespace(
        sender=SimpleNamespace(raw=sender),
        recipient=SimpleNamespace(raw=recipient),
        block=block,
        index=index,
    )


DEV = "0x" + "de" * 20
CEX = "0x" + "ce" * 20
HOLDERS = [f"0x{i:040x}" for i in range(1, 11)]


@pytest.fixture
def scenario():
    """A dev splitting into five, an exchange paying three, and a pair trading."""
    rows = [transfer(DEV, h, 100 + i) for i, h in enumerate(HOLDERS[:5])]
    rows += [transfer(CEX, h, 200 + i) for i, h in enumerate(HOLDERS[5:8])]
    rows += [transfer(CEX, HOLDERS[8], 250), transfer(CEX, HOLDERS[9], 251)]
    rows += [transfer(HOLDERS[8], HOLDERS[9], 300)]
    return [*HOLDERS, DEV], rows


def test_the_dev_and_its_wallets_become_one_party(scenario) -> None:
    groups, _ = group_holders(*scenario)
    biggest = groups[0]
    assert len(biggest.members) == 6
    assert DEV.lower() in biggest.members


def test_an_exchange_does_not_merge_unrelated_holders(scenario) -> None:
    """The failure this analyzer exists to avoid."""
    groups, _ = group_holders(*scenario)
    alone = [g for g in groups if g.alone]
    assert len(alone) == 3, "exchange-funded holders were merged into one party"


def test_a_pair_that_traded_is_grouped(scenario) -> None:
    groups, _ = group_holders(*scenario)
    pair = [g for g in groups if len(g.members) == 2]
    assert pair and {link.kind for link in pair[0].links} == {LinkKind.DIRECT}


def test_a_funder_inside_the_list_is_never_called_a_service(scenario) -> None:
    """One wallet funding eighteen of twenty IS the finding, not noise.

    An earlier version applied the degree test to every funder and then said
    the dev was "treated as a service and not used to link them" --- while
    linking them anyway. The note was false.
    """
    _, notes = group_holders(*scenario)
    dev_notes = [n for n in notes if DEV.lower() in n]
    assert dev_notes, "the dev's role should be reported"
    assert "used to link them" in dev_notes[0]
    assert "not used to link" not in dev_notes[0]


def test_a_third_party_funder_is_named_when_it_is_dropped(scenario) -> None:
    _, notes = group_holders(*scenario)
    cex_notes = [n for n in notes if CEX.lower() in n]
    assert cex_notes and "not used to link them" in cex_notes[0]


def test_alone_is_not_independent() -> None:
    """The wording carries the whole distinction."""
    group = Group(members=["0xabc"])
    assert group.alone
    assert "not evidence of independence" in group.why()


def test_no_transfers_means_no_grouping_not_no_relationship() -> None:
    """An address whose history was never fetched cannot have been linked."""
    groups, _ = group_holders(HOLDERS[:3], [])
    assert len(groups) == 3
    assert all(g.alone for g in groups)


def test_every_group_carries_the_links_that_formed_it(scenario) -> None:
    """A merged group must not look like a strong one when it is not."""
    groups, _ = group_holders(*scenario)
    for group in groups:
        if not group.alone:
            assert group.links, f"{group.members} grouped with no stated reason"
