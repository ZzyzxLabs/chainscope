"""Consolidation analysis.

The tests worth reading are the ones about *not lying*: that truncation is
reported, that incomplete history is reported, and that an unlabelled hub is
never described as if it had been identified.
"""

from datetime import datetime, timezone

import pytest

from chainscope.analysis.base import Context
from chainscope.analysis.consolidation import ConsolidationAnalyzer
from chainscope.attribution.resolver import Resolver
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.core.chainid import ETHEREUM
from chainscope.core.models import Account, Address, Transaction, TxRef
from chainscope.core.result import Severity
from chainscope.core.units import Amount
from chainscope.providers.base import Capability, Provider
from chainscope.providers.router import Router

SEED = "0x1111111111111111111111111111111111111111"
HUB = "0xf1da173228fcf015f43f3ea15abbb51f0d8f1123"
OTHER_HUB = "0xaaaa000000000000000000000000000000000000"


def addr(a: str) -> Address:
    return Address(ETHEREUM, a, a.lower())


def tx(sender: str, recipient: str, eth: str, n: int = 0) -> Transaction:
    return Transaction(
        ref=TxRef(ETHEREUM, f"0x{n:064x}"),
        sender=addr(sender),
        recipient=addr(recipient),
        value=Amount.parse(eth, 18, "ETH"),
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        block=1000 + n,
    )


class FakeProvider(Provider):
    """Serves a canned address -> transactions map."""

    name = "fake"
    chains = frozenset({ETHEREUM})
    capabilities = Capability.ADDRESS_HISTORY | Capability.BALANCE

    def __init__(self, history: dict[str, list[Transaction]], nonce: int | None = None):
        self.history = {k.lower(): v for k, v in history.items()}
        self.nonce = nonce

    def address_history(self, chain, address, **kw):
        return self.history.get(address.lower(), [])

    def get_account(self, chain, address):
        sent = sum(
            1
            for t in self.history.get(address.lower(), [])
            if t.sender and t.sender.key == address.lower()
        )
        return Account(
            address=addr(address),
            tx_count=self.nonce if self.nonce is not None else sent,
        )


def build(deposits: int, *, hub: str = HUB, nonce: int | None = None, eth: str = "10"):
    """Seed sends to N single-use addresses, all forwarding to one hub."""
    dep = [f"0x{i:040x}" for i in range(1, deposits + 1)]
    history: dict[str, list[Transaction]] = {
        SEED: [tx(SEED, d, eth, i) for i, d in enumerate(dep)]
    }
    for i, d in enumerate(dep):
        history[d] = [tx(SEED, d, eth, i), tx(d, hub, eth, 100 + i)]
    return dep, Router([FakeProvider(history, nonce=nonce)])


def ctx_for(router: Router, resolver: Resolver | None = None, **limits) -> Context:
    return Context(chain=ETHEREUM, router=router, resolver=resolver, limits=limits)


class TestDetection:
    def test_detects_a_cluster(self):
        dep, router = build(5)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        (finding,) = [f for f in res.findings if f.data.get("hub")]
        assert finding.data["fan_in"] == 5
        assert sorted(finding.data["deposit_addresses"]) == sorted(dep)
        assert finding.data["total_raw"] == 5 * 10**19

    def test_fan_in_below_threshold_is_not_a_cluster(self):
        """Two addresses sharing a next hop is ordinary coincidence."""
        _, router = build(2)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED, min_fan_in=3)
        assert not [f for f in res.findings if f.data.get("hub")]

    def test_threshold_is_configurable(self):
        _, router = build(2)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED, min_fan_in=2)
        assert [f for f in res.findings if f.data.get("hub")]

    def test_unclustered_destinations_are_reported_separately(self):
        _, router = build(4)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED, min_fan_in=10)
        loose = [f for f in res.findings if "did not cluster" in f.title]
        assert loose and loose[0].data["addresses"]

    def test_no_outgoing_transfers_is_handled(self):
        router = Router([FakeProvider({SEED: []})])
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        assert res.is_empty
        assert any("no outgoing" in w for w in res.warnings)


class TestAttribution:
    def test_labelled_hub_raises_severity_and_names_the_service(self):
        class Labelled(Resolver):
            pass

        class FakeSource:
            name, offline = "fake", True

            class meta:
                max_confidence = Confidence.HIGH

            def ready(self):
                return True

            def lookup(self, address, chain=None):
                if address.lower() != HUB:
                    return []
                return [
                    Attribution(
                        address=address,
                        chain=chain,
                        label="Acme Exchange",
                        category=Category.CEX,
                        confidence=Confidence.HIGH,
                        method=Method.LABEL,
                        source="fake@2026-01-01",
                    )
                ]

            def lookup_many(self, addresses, chain=None):
                return {a: self.lookup(a, chain) for a in addresses}

        _, router = build(4)
        r = Labelled()
        r.sources.append(FakeSource())
        res = ConsolidationAnalyzer().run(ctx_for(router, r), address=SEED)
        (f,) = [f for f in res.findings if f.data.get("hub")]
        assert f.data["label"] == "Acme Exchange"
        assert f.severity is Severity.IMPORTANT
        assert "Acme Exchange" in f.title

    def test_unlabelled_hub_says_so_explicitly(self):
        """Structure shows addresses are related. It does not show to whom."""
        _, router = build(4)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        (f,) = [f for f in res.findings if f.data.get("hub")]
        assert f.data["label"] is None
        assert f.severity is Severity.NOTABLE
        assert "unlabelled" in f.title
        assert "needs an external source" in f.detail


class TestHonesty:
    def test_truncation_is_reported(self):
        """A report saying 'funds reached three services' looks identical
        whether that was the answer or where the search stopped."""
        _, router = build(10)
        res = ConsolidationAnalyzer().run(
            ctx_for(router, max_nodes=4), address=SEED, min_fan_in=3
        )
        assert any("max_nodes limit" in w for w in res.warnings)

    def test_incomplete_history_is_reported(self):
        # Nonce says 50 sends; the provider returned 5.
        _, router = build(5, nonce=50)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        assert any("history is incomplete" in w for w in res.warnings)
        assert any("lower bounds" in w for w in res.warnings)

    def test_complete_history_produces_no_warning(self):
        _, router = build(5)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        assert not any("incomplete" in w for w in res.warnings)

    def test_unreachable_destination_is_reported_not_silently_dropped(self):
        class Flaky(FakeProvider):
            def address_history(self, chain, address, **kw):
                if address.lower().endswith("03"):
                    raise RuntimeError("upstream 502")
                return super().address_history(chain, address, **kw)

        dep = [f"0x{i:040x}" for i in range(1, 5)]
        history = {SEED: [tx(SEED, d, "10", i) for i, d in enumerate(dep)]}
        for i, d in enumerate(dep):
            history[d] = [tx(d, HUB, "10", 100 + i)]
        router = Router([Flaky(history)])
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED, min_fan_in=2)
        assert any("could not be enumerated" in w for w in res.warnings)


class TestReproducibility:
    def test_params_capture_the_run(self):
        _, router = build(4)
        res = ConsolidationAnalyzer().run(
            ctx_for(router), address=SEED, min_fan_in=3, start_block=100
        )
        # A superset: `_result` also records the chain the run used, which a
        # result needs to be reproducible. Asserting equality here would make
        # every future addition to that provenance a test failure, which is the
        # wrong incentive for the thing being encouraged.
        assert (
            res.params.items()
            >= {
                "address": SEED,
                "min_fan_in": 3,
                "start_block": 100,
                "end_block": "latest",
            }.items()
        )
        assert res.params["chain"] == "eip155:1"

    def test_result_serialises(self):
        _, router = build(4)
        res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
        assert '"analyzer": "consolidation"' in res.to_json()


class TestApplicability:
    def test_requires_address_history(self):
        class RpcOnly(Provider):
            name = "rpc"
            chains = frozenset({ETHEREUM})
            capabilities = Capability.BLOCK | Capability.TRANSACTION

        a = ConsolidationAnalyzer()
        assert not a.applicable(ctx_for(Router([RpcOnly()])))
        assert a.applicable(ctx_for(build(3)[1]))


@pytest.mark.parametrize("count", [3, 7, 12])
def test_totals_are_exact_across_sizes(count):
    _, router = build(count, eth="1.5")
    res = ConsolidationAnalyzer().run(ctx_for(router), address=SEED)
    (f,) = [f for f in res.findings if f.data.get("hub")]
    assert f.data["total_raw"] == count * 15 * 10**17
