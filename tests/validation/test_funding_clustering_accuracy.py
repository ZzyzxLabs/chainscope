"""Common-funder clustering, scored against known ownership.

Ethereum has no multi-input heuristic: a transaction has one sender, so no
transaction ever demonstrates two addresses share an owner. The account-model
substitute asks who paid for an address to exist, and that signal has one
structural failure --- exchanges fund their customers.

Measured before the guard was written: twenty operators plus one exchange, and
the exchange's 400 withdrawals collapse into a single cluster asserting that
400 unrelated people are one entity. Not merely wrong; confidently wrong, and
transitive.

Same scoring as the co-spend harness. Precision over recall, because a false
merge asserts strangers are one person and a miss only leaves an address
unclustered.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from chainscope.analysis.funding import (
    SERVICE_FUNDER_DEGREE,
    FundingEvent,
    cluster_by_funder,
    first_funders,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Address, Transfer, TransferKind, TxRef
from chainscope.core.units import Amount

SEED = 20260802
ONE_ETH = 10**18


@dataclass
class World:
    events: list[FundingEvent] = field(default_factory=list)
    owner_of: dict[str, int] = field(default_factory=dict)

    def truth(self) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for address, owner in self.owner_of.items():
            out.setdefault(owner, set()).add(address)
        return out


def build(*, operators: int, per_operator: int, exchange_customers: int) -> World:
    """Operators who fund their own addresses, plus one exchange funding
    strangers. Each customer is their own entity, which is what makes merging
    them a false claim rather than a coarse one."""
    world = World()
    for op in range(operators):
        for i in range(per_operator):
            address = f"0xop{op:02d}addr{i:02d}"
            world.events.append(FundingEvent(address=address, funder=f"0xfunder{op:02d}"))
            world.owner_of[address] = op
    for i in range(exchange_customers):
        address = f"0xcustomer{i:04d}"
        world.events.append(FundingEvent(address=address, funder="0xexchangehot"))
        world.owner_of[address] = 1000 + i
    return world


def _pairs(groups) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for group in groups:
        members = sorted(group)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                out.add((a, b))
    return out


def score(world: World, clusters) -> tuple[float, float, int]:
    """Pairwise precision, recall, and the worst merge."""
    asserted = [c.addresses for c in clusters if c.links_members]
    predicted = _pairs(asserted)
    actual = _pairs(world.truth().values())
    tp = len(predicted & actual)
    fp = len(predicted - actual)
    fn = len(actual - predicted)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    worst = max(
        (len({world.owner_of[a] for a in c}) for c in asserted),
        default=1,
    )
    return precision, recall, worst


class TestItRecoversOperatorsExactly:
    def test_a_world_of_operators_alone(self):
        world = build(operators=20, per_operator=8, exchange_customers=0)
        precision, recall, worst = score(world, cluster_by_funder(world.events))
        assert precision == 1.0
        assert recall == 1.0
        assert worst == 1

    def test_operators_survive_alongside_an_exchange(self):
        """The guard must not cost the signal it is protecting."""
        world = build(operators=20, per_operator=8, exchange_customers=400)
        precision, recall, worst = score(world, cluster_by_funder(world.events))
        assert precision == 1.0
        assert worst == 1
        # Recall is unharmed: the only pairs left unlinked are the exchange's
        # customers, and those were never one entity to begin with.
        assert recall == 1.0
        # Every operator pair is still recovered; only the exchange's
        # customers are left unlinked, and they were never related.
        operators = {c.funder for c in cluster_by_funder(world.events) if c.links_members}
        assert len(operators) == 20


class TestTheExchangeFailureMode:
    def test_without_the_guard_it_merges_strangers(self):
        """The number the default exists for."""
        world = build(operators=20, per_operator=8, exchange_customers=400)
        clusters = cluster_by_funder(world.events, service_degree=10**9)
        precision, _, worst = score(world, clusters)
        assert worst == 400
        assert precision < 0.01

    @pytest.mark.parametrize("customers", [80, 400, 2000])
    def test_the_guard_holds_as_the_exchange_grows(self, customers):
        world = build(operators=10, per_operator=6, exchange_customers=customers)
        precision, _, worst = score(world, cluster_by_funder(world.events))
        assert precision == 1.0
        assert worst == 1

    def test_a_service_cluster_is_kept_but_asserts_nothing(self):
        """Dropping it would hide that a link was considered and declined, and
        "why is this address in no cluster" is a question somebody asks."""
        world = build(operators=2, per_operator=4, exchange_customers=400)
        service = [c for c in cluster_by_funder(world.events) if c.is_service]
        assert len(service) == 1
        assert service[0].size == 400
        assert not service[0].links_members
        assert service[0].attribution(ETHEREUM) is None

    def test_the_summary_explains_the_refusal(self):
        world = build(operators=1, per_operator=2, exchange_customers=200)
        service = next(c for c in cluster_by_funder(world.events) if c.is_service)
        summary = service.summary()
        assert "linked to each other" in summary
        assert "exchange funds its customers" in summary

    def test_a_known_service_is_excluded_regardless_of_degree(self):
        """Better than inferring from degree: a service that funded three
        addresses in the window looks exactly like an operator."""
        world = build(operators=2, per_operator=4, exchange_customers=3)
        clusters = cluster_by_funder(world.events, exclude={"0xexchangehot"})
        exchange = next(c for c in clusters if c.funder == "0xexchangehot")
        assert exchange.is_service
        assert not exchange.links_members


class TestWhatTheClaimSays:
    def test_it_claims_shared_origin_not_shared_control(self):
        """Two addresses funded by one key were set up by whoever held it. They
        may since have been handed to different people."""
        world = build(operators=1, per_operator=5, exchange_customers=0)
        cluster = cluster_by_funder(world.events)[0]
        claim = cluster.attribution(ETHEREUM)
        assert claim is not None
        assert "shared *origin*" in claim.rationale
        assert "weaker than shared control" in claim.rationale

    def test_it_never_exceeds_medium(self):
        world = build(operators=1, per_operator=40, exchange_customers=0)
        claim = cluster_by_funder(world.events)[0].attribution(ETHEREUM)
        assert claim is not None
        assert claim.confidence <= Confidence.MEDIUM

    def test_a_cluster_of_one_asserts_nothing(self):
        events = [FundingEvent(address="0xa", funder="0xf")]
        cluster = cluster_by_funder(events)[0]
        assert not cluster.links_members
        assert cluster.attribution(ETHEREUM) is None

    def test_the_threshold_is_reported_rather_than_applied_silently(self):
        """It is a cut on a continuum. An operator running a large campaign and
        an exchange having a quiet day meet somewhere near it."""
        world = build(operators=0, per_operator=0, exchange_customers=SERVICE_FUNDER_DEGREE + 1)
        cluster = cluster_by_funder(world.events)[0]
        assert cluster.is_service
        assert "more than" in cluster.summary()


class TestDerivingEventsFromTransfers:
    def _transfer(self, sender, recipient, *, block, index=0):
        return Transfer(
            chain=ETHEREUM,
            tx=TxRef(ETHEREUM, f"0x{block:064x}"),
            sender=Address(ETHEREUM, sender, sender),
            recipient=Address(ETHEREUM, recipient, recipient),
            amount=Amount(ONE_ETH, 18, "ETH"),
            kind=TransferKind.NATIVE,
            block=block,
            index=index,
        )

    def test_only_the_first_inbound_transfer_counts(self):
        """Later ones say nothing about origin: by then the address exists and
        anybody can pay it."""
        events = first_funders(
            [
                self._transfer("0xfunder", "0xnew", block=100),
                self._transfer("0xsomebodyelse", "0xnew", block=200),
            ]
        )
        assert [e.funder for e in events] == ["0xfunder"]

    def test_ordering_is_by_block_then_index(self):
        events = first_funders(
            [
                self._transfer("0xsecond", "0xnew", block=100, index=5),
                self._transfer("0xfirst", "0xnew", block=100, index=1),
            ]
        )
        assert [e.funder for e in events] == ["0xfirst"]

    def test_a_self_transfer_funds_nothing(self):
        """Otherwise every address becomes its own funder, producing clusters
        of one that look like findings."""
        assert first_funders([self._transfer("0xa", "0xa", block=1)]) == []

    def test_an_event_needs_both_sides(self):
        with pytest.raises(ValueError, match="address and a funder"):
            FundingEvent(address="0xa", funder="")


class TestScale:
    def test_a_realistic_mixture(self):
        """Operators of varying size, two exchanges, and some noise."""
        rng = random.Random(SEED)
        world = World()
        for op in range(30):
            size = rng.randint(2, 25)
            for i in range(size):
                address = f"0xop{op:02d}_{i:02d}"
                world.events.append(FundingEvent(address=address, funder=f"0xf{op:02d}"))
                world.owner_of[address] = op
        for e, count in enumerate((900, 300)):
            for i in range(count):
                address = f"0xcust{e}_{i:04d}"
                world.events.append(FundingEvent(address=address, funder=f"0xexchange{e}"))
                world.owner_of[address] = 10_000 + e * 10_000 + i

        precision, recall, worst = score(world, cluster_by_funder(world.events))
        assert precision == 1.0
        assert worst == 1
        # Recall is below 1 only because the exchange customers are correctly
        # not linked --- and they were never one entity to begin with.
        assert recall == 1.0
