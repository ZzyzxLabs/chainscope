"""Etherscan provider.

The tests that matter are about the three response shapes. Etherscan uses the
same `status: "0"` for "this address has no transactions" and "you are rate
limited", and conflating them makes an address silently vanish from an
analysis — which is the single failure this project is built around preventing.
"""

import pytest

from chainscope.core.chainid import BITCOIN, BSC, ETHEREUM
from chainscope.providers.base import Capability, ProviderError
from chainscope.providers.etherscan import (
    END_OF_CHAIN,
    MAX_RECORDS,
    EtherscanProvider,
    ResultTruncated,
)

ADDR = "0x28c6c06298d514db089934071355e5743bf21d60"
OTHER = "0xf977814e90da44bfa03b6295a0616a897441acec"


class FakeClient:
    """Serves canned bodies keyed by the `action` parameter."""

    def __init__(self, bodies: dict[str, object]):
        self.bodies = bodies
        self.calls: list[dict] = []

    def get(self, url, params=None, **kw):
        self.calls.append(dict(params or {}))
        action = (params or {}).get("action")
        body = self.bodies.get(action, {"status": "0", "message": "No transactions found"})
        if isinstance(body, Exception):
            raise body
        return body


def tx_row(**kw):
    base = {
        "hash": "0x" + "a" * 64,
        "from": ADDR,
        "to": OTHER,
        "value": "1000000000000000000",
        "timeStamp": "1767225600",
        "blockNumber": "21000000",
        "isError": "0",
        "nonce": "5",
        "input": "0x",
    }
    return {**base, **kw}


def token_row(**kw):
    base = {
        "hash": "0x" + "b" * 64,
        "from": ADDR,
        "to": OTHER,
        "value": "1000000",
        "timeStamp": "1767225600",
        "blockNumber": "21000001",
        "tokenSymbol": "USDC",
        "tokenDecimal": "6",
        "contractAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    }
    return {**base, **kw}


def provider(bodies) -> EtherscanProvider:
    return EtherscanProvider("test-key", client=FakeClient(bodies))


class TestConstruction:
    def test_key_is_required(self):
        with pytest.raises(ValueError, match="needs an API key"):
            EtherscanProvider("")

    def test_declares_address_history(self):
        """The capability that makes this provider worth having."""
        p = provider({})
        assert p.capabilities.covers(Capability.ADDRESS_HISTORY)
        assert p.supports(ETHEREUM, Capability.ADDRESS_HISTORY)

    def test_non_evm_chain_is_rejected(self):
        p = EtherscanProvider("k", frozenset({BITCOIN}), client=FakeClient({}))
        with pytest.raises(ProviderError, match="not an EVM chain"):
            p.address_history(BITCOIN, ADDR)

    def test_chain_id_is_passed_through(self):
        """One key, sixty chains -- the V2 endpoint switches on chainid."""
        p = EtherscanProvider("k", frozenset({BSC}), client=FakeClient({}))
        p.address_history(BSC, ADDR)
        assert p.client.calls[0]["chainid"] == 56


class TestResponseShapes:
    def test_empty_result_is_data_not_failure(self):
        """'No transactions found' is a fact about the chain."""
        p = provider({"txlist": {"status": "0", "message": "No transactions found"}})
        assert p.address_history(ETHEREUM, ADDR) == []

    @pytest.mark.parametrize(
        "message",
        [
            "NOTOK",
            "Max rate limit reached",
            "Invalid API Key",
        ],
    )
    def test_an_error_raises_rather_than_returning_empty(self, message):
        """The failure this design exists to prevent.

        An empty list here removes the address from the analysis with nothing
        anywhere to indicate it was ever asked about.
        """
        p = provider({"txlist": {"status": "0", "message": message, "result": "detail"}})
        with pytest.raises(ProviderError, match="txlist"):
            p.address_history(ETHEREUM, ADDR)

    def test_error_message_carries_the_detail(self):
        p = provider(
            {"txlist": {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}}
        )
        with pytest.raises(ProviderError, match="Max rate limit reached"):
            p.address_history(ETHEREUM, ADDR)

    def test_api_cap_is_reported_not_silently_returned(self):
        """Etherscan caps at 10,000 records and says so nowhere.

        Returning them quietly produces a confident total that is simply wrong.
        """
        p = provider({"txlist": {"status": "1", "result": [tx_row()] * MAX_RECORDS}})
        with pytest.raises(ResultTruncated, match="lower bound"):
            p.address_history(ETHEREUM, ADDR)

    def test_filling_the_requested_limit_also_counts_as_truncated(self):
        """A caller asking for 5 and receiving 5 is truncated too.

        Checking only against the API cap lets this pass silently, which is the
        failure mode ResultTruncated exists to make impossible.
        """
        p = provider({"txlist": {"status": "1", "result": [tx_row()] * 5}})
        with pytest.raises(ResultTruncated, match="exactly the number requested"):
            p.address_history(ETHEREUM, ADDR, limit=5)

    def test_under_the_limit_is_complete(self):
        p = provider({"txlist": {"status": "1", "result": [tx_row()] * 3}})
        assert len(p.address_history(ETHEREUM, ADDR, limit=5)) == 3

    def test_api_cap_message_says_so(self):
        p = provider({"txlist": {"status": "1", "result": [tx_row()] * MAX_RECORDS}})
        with pytest.raises(ResultTruncated, match="API maximum"):
            p.address_history(ETHEREUM, ADDR, limit=MAX_RECORDS)

    def test_truncation_is_a_distinct_type(self):
        """Callers handle 'incomplete but usable' differently from 'failed'."""
        assert issubclass(ResultTruncated, ProviderError)

    def test_malformed_body_is_rejected(self):
        p = provider({"txlist": "not a dict"})
        with pytest.raises(ProviderError, match="unexpected response shape"):
            p.address_history(ETHEREUM, ADDR)


class TestAddressHistory:
    def test_parses_a_transaction(self):
        p = provider({"txlist": {"status": "1", "result": [tx_row()]}})
        (tx,) = p.address_history(ETHEREUM, ADDR)
        assert tx.sender.key == ADDR
        assert tx.value.raw == 10**18
        assert tx.block == 21_000_000
        assert tx.timestamp.year == 2026
        assert tx.success

    def test_reverted_transactions_are_marked(self):
        """A revert moved no value; counting it inflates every total."""
        p = provider({"txlist": {"status": "1", "result": [tx_row(isError="1")]}})
        (tx,) = p.address_history(ETHEREUM, ADDR)
        assert not tx.success

    def test_latest_becomes_a_concrete_end_block(self):
        """Etherscan documents endblock as an integer, so "latest" cannot pass
        through. The sentinel must also outlast fast chains: BSC at three
        seconds a block would reach the common 99,999,999 idiom around 2030."""
        p = provider({"txlist": {"status": "1", "result": []}})
        p.address_history(ETHEREUM, ADDR, end_block="latest")
        assert p.client.calls[0]["endblock"] == END_OF_CHAIN
        assert END_OF_CHAIN > 500_000_000


class TestAssetTransfers:
    def _bodies(self):
        return {
            "txlist": {"status": "1", "result": [tx_row()]},
            "txlistinternal": {
                "status": "1",
                "result": [tx_row(hash="0x" + "c" * 64, blockNumber="21000002")],
            },
            "tokentx": {"status": "1", "result": [token_row()]},
        }

    def test_includes_internal_transfers(self):
        """They produce no log and no top-level transaction, so a tracer
        reading only the other two misses swap proceeds and payouts."""
        p = provider(self._bodies())
        kinds = {t.kind.value for t in p.asset_transfers(ETHEREUM, ADDR, direction="all")}
        assert kinds == {"native", "internal", "token"}

    def test_token_decimals_are_respected(self):
        p = provider(self._bodies())
        usdc = [
            t
            for t in p.asset_transfers(ETHEREUM, ADDR, direction="all")
            if t.amount.symbol == "USDC"
        ]
        assert usdc[0].amount.decimals == 6
        assert str(usdc[0].amount) == "1 USDC"

    def test_direction_out(self):
        p = provider(self._bodies())
        got = p.asset_transfers(ETHEREUM, ADDR, direction="out")
        assert got and all(t.sender.key == ADDR for t in got)

    def test_direction_in_filters_everything_out_here(self):
        p = provider(self._bodies())
        assert p.asset_transfers(ETHEREUM, ADDR, direction="in") == []

    def test_zero_value_transfers_are_dropped(self):
        p = provider({"txlist": {"status": "1", "result": [tx_row(value="0")]}})
        assert p.asset_transfers(ETHEREUM, ADDR, direction="all") == []

    def test_one_failing_endpoint_does_not_lose_the_others(self):
        bodies = self._bodies()
        bodies["tokentx"] = {"status": "0", "message": "NOTOK", "result": "boom"}
        p = provider(bodies)
        kinds = {t.kind.value for t in p.asset_transfers(ETHEREUM, ADDR, direction="all")}
        assert kinds == {"native", "internal"}

    def test_truncation_still_propagates_through_the_loop(self):
        """A partial answer must not be dressed up as a complete one."""
        bodies = self._bodies()
        bodies["txlist"] = {"status": "1", "result": [tx_row()] * MAX_RECORDS}
        with pytest.raises(ResultTruncated):
            provider(bodies).asset_transfers(ETHEREUM, ADDR, direction="all")

    def test_results_are_block_ordered(self):
        p = provider(self._bodies())
        got = p.asset_transfers(ETHEREUM, ADDR, direction="all")
        assert [t.block for t in got] == sorted(t.block for t in got)


class TestCredentialHandling:
    def test_key_is_sent_but_never_in_an_error_message(self):
        p = provider({"txlist": {"status": "0", "message": "NOTOK", "result": "x"}})
        with pytest.raises(ProviderError) as exc:
            p.address_history(ETHEREUM, ADDR)
        assert "test-key" not in str(exc.value)
        assert p.client.calls[0]["apikey"] == "test-key"
