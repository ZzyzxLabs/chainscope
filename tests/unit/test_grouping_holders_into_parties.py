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
    assert cex_notes, "a dropped funder must be named, not silently ignored"
    # The claim, not the wording. Either phrasing is true --- "not used to link
    # them" and "used as a boundary: the walk stopped there" say the same
    # thing, and the second says more.
    note = cex_notes[0]
    assert "treated as a service" in note
    assert "not used to link them" in note or "stopped there" in note
    assert "funded 5 addresses" in note, "the degree is the reason; state it"


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


# ---------------------------------------------------------------- layering
#
# The shape that actually occurs: split, layer, layer, then out to an
# exchange. One hop finds none of it; unlimited hops find everybody, because
# three hops from almost any address reaches a hot wallet that paid millions.


DEP = "0x" + "d0" * 20
L1 = ["0x" + f"{i:02x}" * 20 for i in (0xA1, 0xA2)]
L2 = ["0x" + f"{i:02x}" * 20 for i in (0xB1, 0xB2)]
SYNDICATE = [f"0x{i:040x}" for i in range(1, 5)]
CIVILIANS = [f"0x{i:040x}" for i in range(90, 96)]
FRIENDS = [f"0x{i:040x}" for i in (0x71, 0x72)]


def paid(sender: str, recipient: str, block: int, amount: int = 10**18):
    row = transfer(sender, recipient, block)
    row.amount = SimpleNamespace(raw=amount)
    return row


@pytest.fixture
def layered():
    rows = [paid(DEV, a, 100 + i) for i, a in enumerate(L1)]
    rows += [paid(L1[i], b, 200 + i) for i, b in enumerate(L2)]
    rows += [paid(L2[i // 2], h, 300 + i) for i, h in enumerate(SYNDICATE)]
    # An exchange paying sixty people at scattered times in scattered amounts.
    rows += [paid(CEX, f"0x{0xF00 + i:040x}", 400 + i * 7, (i + 1) * 10**17) for i in range(60)]
    rows += [paid(CEX, h, 500 + i * 97, (i + 3) * 10**17) for i, h in enumerate(CIVILIANS)]
    # Two people depositing into one exchange deposit address.
    rows += [paid(CEX, h, 600 + i * 50, (i + 9) * 10**17) for i, h in enumerate(FRIENDS)]
    rows += [paid(FRIENDS[0], DEP, 700), paid(FRIENDS[1], DEP, 760), paid(DEP, CEX, 800)]
    return SYNDICATE + CIVILIANS + FRIENDS, rows


def test_three_layers_do_not_hide_the_syndicate(layered) -> None:
    groups, _ = group_holders(*layered, max_depth=3)
    biggest = max(groups, key=lambda g: len(g.members))
    assert set(biggest.members) == {a.lower() for a in SYNDICATE}


def test_the_exchange_does_not_pull_everyone_in(layered) -> None:
    """Three hops reaches a hot wallet from almost anywhere. Stopping there is
    the difference between a finding and an artefact shaped like a whale."""
    groups, _ = group_holders(*layered, max_depth=3)
    biggest = max(groups, key=lambda g: len(g.members))
    assert not any(c.lower() in biggest.members for c in CIVILIANS)


def test_ordinary_withdrawals_stay_separate(layered) -> None:
    """They came from one exchange minutes apart in similar amounts, which is
    what a deliberate split also looks like --- until the windows are tight."""
    groups, _ = group_holders(*layered, max_depth=3)
    civil = {c.lower() for c in CIVILIANS}
    merged = [g for g in groups if len(set(g.members) & civil) > 1]
    assert not merged, f"unrelated withdrawals were merged: {merged}"


def test_a_shared_deposit_address_links_its_payers(layered) -> None:
    """An exchange deposit address belongs to one customer. Two addresses
    paying into it are that customer, or somebody acting for them."""
    groups, _ = group_holders(*layered, max_depth=3)
    friends = {f.lower() for f in FRIENDS}
    found = [g for g in groups if set(g.members) == friends]
    assert found, "addresses sharing a deposit address were not linked"
    assert any(link.kind is LinkKind.SHARED_DEPOSIT for link in found[0].links)


def test_a_hot_wallet_is_not_mistaken_for_a_deposit_address(layered) -> None:
    """The shapes are mirror images: a deposit address takes from few and
    forwards to a service; a hot wallet takes from few and pays many."""
    groups, _ = group_holders(*layered, max_depth=3)
    for group in groups:
        for link in group.links:
            if link.kind is LinkKind.SHARED_DEPOSIT:
                assert CEX.lower() not in link.detail


def test_depth_is_recorded_so_a_far_link_is_not_read_as_a_near_one(layered) -> None:
    groups, _ = group_holders(*layered, max_depth=3)
    biggest = max(groups, key=lambda g: len(g.members))
    assert any(link.depth > 1 for link in biggest.links)
    assert all(link.depth <= 3 for g in groups for link in g.links)
