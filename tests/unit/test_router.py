"""Capability routing and read-only enforcement."""

import pytest

from chainscope.core.chainid import BITCOIN, BSC, ETHEREUM
from chainscope.providers.base import Capability, CostTier, Provider, ProviderError
from chainscope.providers.router import NoProviderError, Router
from chainscope.transport.http import ReadOnlyViolation, assert_read_only


def make(name, chains, caps, cost=CostTier.FREE_PUBLIC, fails=False):
    class Fake(Provider):
        def get_block(self, chain, number):
            if fails:
                raise ProviderError("upstream down")
            return f"{self.name}:{number}"

    p = Fake()
    p.name = name
    p.chains = frozenset(chains)
    p.capabilities = caps
    p.cost = cost
    return p


class TestCapabilities:
    def test_covers_is_subset_not_equality(self):
        rich = Capability.BLOCK | Capability.LOGS | Capability.TRACE
        assert rich.covers(Capability.BLOCK)
        assert rich.covers(Capability.BLOCK | Capability.LOGS)
        assert not rich.covers(Capability.ADDRESS_HISTORY)

    def test_supports_requires_both_chain_and_capability(self):
        p = make("x", [ETHEREUM], Capability.BLOCK)
        assert p.supports(ETHEREUM, Capability.BLOCK)
        assert not p.supports(BSC, Capability.BLOCK)
        assert not p.supports(ETHEREUM, Capability.TRACE)


class TestSelection:
    def test_cheapest_tier_wins(self):
        """A paid quota should not be spent on what a public node answers."""
        paid = make("paid", [ETHEREUM], Capability.BLOCK, CostTier.PAID)
        free = make("free", [ETHEREUM], Capability.BLOCK, CostTier.FREE_PUBLIC)
        r = Router([paid, free])
        assert [p.name for p in r.candidates(ETHEREUM, Capability.BLOCK)] == ["free", "paid"]

    def test_local_node_outranks_everything(self):
        local = make("local", [ETHEREUM], Capability.BLOCK, CostTier.LOCAL)
        free = make("free", [ETHEREUM], Capability.BLOCK, CostTier.FREE_PUBLIC)
        r = Router([free, local])
        assert r.candidates(ETHEREUM, Capability.BLOCK)[0].name == "local"

    def test_explicit_preference_overrides_cost(self):
        paid = make("paid", [ETHEREUM], Capability.BLOCK, CostTier.PAID)
        free = make("free", [ETHEREUM], Capability.BLOCK, CostTier.FREE_PUBLIC)
        r = Router([free, paid], preferred=["paid"])
        assert r.candidates(ETHEREUM, Capability.BLOCK)[0].name == "paid"

    def test_ordering_is_deterministic_for_equivalent_providers(self):
        a = make("a", [ETHEREUM], Capability.BLOCK)
        b = make("b", [ETHEREUM], Capability.BLOCK)
        r = Router([a, b])
        assert [p.name for p in r.candidates(ETHEREUM, Capability.BLOCK)] == ["a", "b"]


class TestDispatch:
    def test_falls_back_past_a_failing_provider(self):
        broken = make("broken", [ETHEREUM], Capability.BLOCK, fails=True)
        working = make("working", [ETHEREUM], Capability.BLOCK)
        r = Router([broken, working])
        assert (
            r.dispatch(ETHEREUM, Capability.BLOCK, lambda p: p.get_block(ETHEREUM, 1))
            == "working:1"
        )

    def test_reports_every_failure_when_all_fail(self):
        r = Router(
            [
                make("a", [ETHEREUM], Capability.BLOCK, fails=True),
                make("b", [ETHEREUM], Capability.BLOCK, fails=True),
            ]
        )
        with pytest.raises(ProviderError, match="all 2 providers failed"):
            r.dispatch(ETHEREUM, Capability.BLOCK, lambda p: p.get_block(ETHEREUM, 1))

    def test_a_bug_in_our_code_is_not_masked_by_fallback(self):
        """Only ProviderError means 'try someone else'.

        A ValueError while parsing means the provider answered and we mishandled
        it. Retrying elsewhere would hide our bug behind another response.
        """

        def explode(_):
            raise ValueError("bad parse")

        r = Router([make("only", [ETHEREUM], Capability.BLOCK)])
        with pytest.raises(ValueError, match="bad parse"):
            r.dispatch(ETHEREUM, Capability.BLOCK, explode)


class TestDiagnostics:
    def test_missing_capability_message_is_actionable(self):
        r = Router([make("rpc", [ETHEREUM], Capability.BLOCK)])
        with pytest.raises(NoProviderError) as e:
            r.dispatch(ETHEREUM, Capability.ADDRESS_HISTORY, lambda p: None)
        msg = str(e.value)
        assert "ADDRESS_HISTORY" in msg
        assert "rpc" in msg  # names what *is* configured
        assert "plain RPC cannot" in msg  # explains why, not just that

    def test_unconfigured_chain_says_so(self):
        r = Router([make("rpc", [ETHEREUM], Capability.BLOCK)])
        with pytest.raises(NoProviderError, match="No provider is configured"):
            r.dispatch(BITCOIN, Capability.BLOCK, lambda p: None)

    def test_capabilities_for_unions_across_providers(self):
        r = Router(
            [
                make("rpc", [ETHEREUM], Capability.BLOCK | Capability.LOGS),
                make("explorer", [ETHEREUM], Capability.ADDRESS_HISTORY),
            ]
        )
        caps = r.capabilities_for(ETHEREUM)
        assert caps.covers(Capability.BLOCK | Capability.ADDRESS_HISTORY)


class TestReadOnly:
    @pytest.mark.parametrize(
        "method",
        [
            "eth_sendRawTransaction",
            "eth_sendTransaction",
            "eth_sign",
            "eth_signTransaction",
            "personal_unlockAccount",
            "personal_sendTransaction",
            "miner_start",
            "admin_addPeer",
            "eth_accounts",
        ],
    )
    def test_write_and_signing_methods_are_blocked(self, method):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(method)

    @pytest.mark.parametrize(
        "method",
        [
            "eth_call",
            "eth_getLogs",
            "eth_getBalance",
            "eth_getTransactionByHash",
            "eth_getStorageAt",
            "debug_traceTransaction",
            "trace_block",
        ],
    )
    def test_read_methods_pass(self, method):
        assert_read_only(method)

    def test_blocking_is_case_insensitive(self):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only("ETH_SENDRAWTRANSACTION")

    def test_error_points_the_user_somewhere_useful(self):
        with pytest.raises(ReadOnlyViolation, match="you want a wallet"):
            assert_read_only("eth_sendTransaction")
