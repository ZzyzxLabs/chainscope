"""Co-spend clustering, measured against known ownership.

The multi-input heuristic --- two addresses spending together are controlled by
one entity --- is the oldest tool in this field (Meiklejohn et al., *A Fistful
of Bitcoins*, IMC 2013; Reid & Harrigan, 2013). It is also the one everybody
cites and nobody measures on their own data, which is a poor combination for
something whose output goes into a report.

So this builds worlds where ownership is known by construction, runs the
analyzer, and asserts bounds on precision and recall. Unlike the unit tests,
these exist to keep a *quality* guarantee rather than a behavioural one: they
would fail if somebody made the clustering more aggressive without noticing
what it cost.

**Precision matters more than recall here, which is the reverse of most
classification work.** A false merge asserts that two strangers are one person,
and because clustering is transitive the error does not stay local --- it joins
their whole neighbourhoods. A miss only leaves an address unclustered, and
somebody can look again.

Measured on these worlds, with a single CoinJoin present:

    defence ON     precision 100%
    defence OFF    precision  45%,  worst cluster merges 5 unrelated wallets

At ten CoinJoins the undefended version reaches 5% precision and merges
eighteen of twenty wallets into one cluster. That is the number behind
``skip_coinjoins=True`` being the default.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from chainscope.analysis.cluster import CoSpendClusterAnalyzer, looks_like_coinjoin

SEED = 20260802


@dataclass
class Tx:
    txid: str
    input_addresses: list[str]
    output_values: list[int]


@dataclass
class World:
    """A synthetic chain whose ownership is known because we assigned it."""

    owner_of: dict[str, int] = field(default_factory=dict)
    txs: list[Tx] = field(default_factory=list)

    def truth(self) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for address, owner in self.owner_of.items():
            out.setdefault(owner, set()).add(address)
        return out


class _Ctx:
    """The minimum an Analyzer requires. No evidence: these worlds have no
    recorded responses, and what is under test is the heuristic."""

    def evidence(self) -> list[object]:
        return []


class _Walker:
    def __init__(self, world: World) -> None:
        self.by_address: dict[str, list[Tx]] = {}
        for tx in world.txs:
            for address in tx.input_addresses:
                self.by_address.setdefault(address, []).append(tx)

    def spending_transactions(self, address: str) -> list[Tx]:
        return self.by_address.get(address, [])


def build(*, wallets: int, per_wallet: int, spends: int, coinjoins: int, seed: int) -> World:
    """Independent wallets that co-spend their own addresses, plus CoinJoins.

    The intra-wallet co-spends are the signal the heuristic is meant to find.
    A CoinJoin puts addresses from several wallets into one transaction's
    inputs, which is precisely the input the heuristic's assumption is false
    for.
    """
    rng = random.Random(seed)
    world = World()
    owned: dict[int, list[str]] = {}
    for w in range(wallets):
        addrs = [f"w{w}a{i}" for i in range(per_wallet)]
        owned[w] = addrs
        for a in addrs:
            world.owner_of[a] = w

    n = 0
    for addrs in owned.values():
        for _ in range(spends):
            inputs = rng.sample(addrs, rng.randint(2, min(len(addrs), 4)))
            # Payment plus change, deliberately unequal so the CoinJoin
            # detector has no reason to fire.
            world.txs.append(
                Tx(f"tx{n}", inputs, [rng.randint(10_000, 900_000), rng.randint(1, 9_000)])
            )
            n += 1

    for _ in range(coinjoins):
        participants = rng.sample(list(owned), min(5, wallets))
        inputs = [rng.choice(owned[w]) for w in participants]
        equal = rng.choice([100_000, 1_000_000])
        world.txs.append(Tx(f"cj{n}", inputs, [equal] * 5 + [rng.randint(1, 999)]))
        n += 1

    return world


def cluster_all(world: World, *, skip_coinjoins: bool) -> list[set[str]]:
    analyzer = CoSpendClusterAnalyzer(_Walker(world))
    seen: set[str] = set()
    clusters: list[set[str]] = []
    for address in sorted(world.owner_of):
        if address in seen:
            continue
        result = analyzer.run(
            _Ctx(),
            address=address,
            skip_coinjoins=skip_coinjoins,
            max_addresses=10_000,
            max_transactions=100_000,
        )
        members = set(result.findings[0].data.get("addresses") or [address])
        clusters.append(members)
        seen |= members
    return clusters


def _world_with_a_join() -> tuple[World, str]:
    """A world plus an address that actually took part in a CoinJoin.

    Seeding from an arbitrary address would often find nothing to skip, and a
    test that passes because it exercised nothing is worse than no test.
    """
    world = build(wallets=6, per_wallet=4, spends=6, coinjoins=4, seed=SEED + 3)
    joined = next(tx.input_addresses[0] for tx in world.txs if tx.txid.startswith("cj"))
    return world, joined


def _pairs(groups) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for group in groups:
        members = sorted(group)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                out.add((a, b))
    return out


def score(world: World, clusters: list[set[str]]) -> tuple[float, float, int]:
    """Pairwise precision, recall, and the worst merge.

    Pairwise because that is what the claim actually is: for each pair of
    addresses, did we say "same entity", and were we right. One bad merge of
    two large wallets is then penalised in proportion to how many people it
    wrongly linked, which is the real cost.
    """
    predicted = _pairs(clusters)
    actual = _pairs(world.truth().values())
    tp = len(predicted & actual)
    fp = len(predicted - actual)
    fn = len(actual - predicted)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    worst = max((len({world.owner_of[a] for a in c}) for c in clusters), default=1)
    return precision, recall, worst


class TestTheHeuristicWorksWhenItsAssumptionHolds:
    def test_a_clean_world_is_recovered_exactly(self):
        world = build(wallets=20, per_wallet=6, spends=8, coinjoins=0, seed=SEED)
        precision, recall, worst = score(world, cluster_all(world, skip_coinjoins=True))
        assert precision == 1.0
        assert recall == 1.0
        assert worst == 1

    def test_the_defence_costs_no_recall_when_there_is_nothing_to_defend(self):
        """Skipping is worth nothing if it also skips ordinary transactions."""
        world = build(wallets=20, per_wallet=6, spends=8, coinjoins=0, seed=SEED)
        on = score(world, cluster_all(world, skip_coinjoins=True))
        off = score(world, cluster_all(world, skip_coinjoins=False))
        assert on == off


class TestCoinJoinIsTheFailureModeTheLiteratureNames:
    @pytest.mark.parametrize("coinjoins", [1, 3, 10])
    def test_the_defence_holds_precision(self, coinjoins):
        world = build(
            wallets=20, per_wallet=6, spends=8, coinjoins=coinjoins, seed=SEED + coinjoins
        )
        precision, recall, worst = score(world, cluster_all(world, skip_coinjoins=True))
        assert precision == 1.0, f"{coinjoins} CoinJoin(s) leaked into the clusters"
        assert recall == 1.0
        assert worst == 1

    def test_one_coinjoin_alone_halves_undefended_precision(self):
        """The number behind skip_coinjoins defaulting to True."""
        world = build(wallets=20, per_wallet=6, spends=8, coinjoins=1, seed=SEED + 1)
        precision, _, worst = score(world, cluster_all(world, skip_coinjoins=False))
        assert precision < 0.6
        assert worst >= 5

    def test_undefended_clustering_collapses_at_scale(self):
        """Ten collaborative spends merge most of the world into one entity."""
        world = build(wallets=20, per_wallet=6, spends=8, coinjoins=10, seed=SEED + 10)
        precision, _, worst = score(world, cluster_all(world, skip_coinjoins=False))
        assert precision < 0.15
        assert worst >= 15

    def test_skipped_transactions_are_named_not_merely_omitted(self):
        """A cluster that quietly declined to follow a transaction has a
        boundary nobody can account for. The skipped txids are reported, so
        the gap is inspectable rather than inferred from a smaller answer."""
        world, joined = _world_with_a_join()
        result = CoSpendClusterAnalyzer(_Walker(world)).run(
            _Ctx(), address=joined, skip_coinjoins=True, max_transactions=100_000
        )
        skipped = result.findings[0].data["skipped_coinjoins"]
        assert skipped, "a CoinJoin was skipped but not reported"
        assert all(txid.startswith("cj") for txid in skipped)

    def test_nothing_is_skipped_when_the_defence_is_off(self):
        """The other half: the field is not merely always populated."""
        world, joined = _world_with_a_join()
        result = CoSpendClusterAnalyzer(_Walker(world)).run(
            _Ctx(), address=joined, skip_coinjoins=False, max_transactions=100_000
        )
        assert result.findings[0].data["skipped_coinjoins"] == []


class TestTheDetector:
    @pytest.mark.parametrize(
        ("label", "inputs", "outputs", "expected"),
        [
            ("Wasabi-style, 5 in / 5 equal out", 5, [100_000] * 5 + [7], True),
            ("JoinMarket, 6 in / 6 equal out", 6, [50_000] * 6 + [12, 44], True),
            ("ordinary payment with change", 2, [90_000, 3_412], False),
            ("batched payout, one input", 1, [i * 1000 for i in range(1, 9)], False),
            ("consolidation, 9 in / 1 out", 9, [800_000], False),
            # Fewer inputs than equal outputs: an exchange paying several
            # customers the same amount, which is not a collaborative spend.
            ("exchange batch, 3 in / 5 equal out", 3, [10_000] * 5, False),
        ],
    )
    def test_shapes(self, label, inputs, outputs, expected):
        assert looks_like_coinjoin(inputs, outputs) is expected, label

    def test_it_errs_toward_suspicion(self):
        """Skipping a real transaction loses a little coverage. Clustering
        through a real CoinJoin merges unrelated people, transitively."""
        assert looks_like_coinjoin(5, [1] * 5)
