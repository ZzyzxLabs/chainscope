"""Read-only by construction --- checked, not asserted.

The README says signing and broadcasting are blocked in the transport layer
"not by convention", and `assert_payload_read_only`'s own docstring says it
raises if a mutating operation is named **anywhere** in a request body.

It checked a top-level list and one level of dict keys. So
`{"requests": [{"method": "eth_sendRawTransaction"}]}` --- an ordinary batching
gateway shape --- went straight through, as did anything one dict deeper. Found
by a full-repo review; reproduced before it was fixed.

A safety property that is written down and not enforced is worse than one
nobody claimed, because the claim is what people rely on.
"""

from __future__ import annotations

import pytest

from chainscope.transport.http import (
    ReadOnlyViolation,
    assert_payload_read_only,
    assert_read_only,
)

MUTATING = "eth_sendRawTransaction"


class TestMutatingCallsAreBlockedAtEveryDepth:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"method": MUTATING}, id="plain"),
            pytest.param([{"method": MUTATING}], id="top-level batch"),
            pytest.param({"requests": [{"method": MUTATING}]}, id="nested batch"),
            pytest.param({"body": {"method": MUTATING}}, id="one dict deep"),
            pytest.param({"a": {"b": [{"method": MUTATING}]}}, id="two deep"),
            pytest.param({MUTATING: {"params": []}}, id="keyed by method name"),
            pytest.param({"b": [MUTATING]}, id="bare string in a list"),
            pytest.param({"b": (MUTATING,)}, id="in a tuple"),
            pytest.param({"b": {MUTATING}}, id="in a set"),
        ],
    )
    def test_it_raises(self, payload: object) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_payload_read_only(payload)

    def test_the_error_names_the_string(self) -> None:
        # So it can be diagnosed in one look rather than by bisecting a payload.
        with pytest.raises(ReadOnlyViolation, match=MUTATING):
            assert_payload_read_only({"a": {"b": [{"method": MUTATING}]}})


class TestOrdinaryReadsAreUntouched:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                {"method": "eth_getLogs", "params": [{"fromBlock": "0x1"}]}, id="logs"
            ),
            pytest.param({"a": {"b": [{"method": "eth_getBalance"}]}}, id="nested read"),
            pytest.param(
                {"method": "eth_getLogs", "params": [{"topics": ["0x" + "d" * 64]}]},
                id="topic hashes",
            ),
            pytest.param({"method": "eth_call", "params": []}, id="eth_call"),
            pytest.param([], id="empty"),
            pytest.param({"jsonrpc": "2.0", "id": 1}, id="no method at all"),
        ],
    )
    def test_it_passes(self, payload: object) -> None:
        assert_payload_read_only(payload)


class TestTheGuardCoversEveryChainThisSupports:
    """It was EVM-shaped, and only partly that.

    Measured before the fix: of ten mutating methods across the five supported
    ecosystems, exactly **one** was blocked. `parity_setr` was a typo for
    `parity_set` and matched no method that exists; Solana, Sui and Tron were
    absent entirely.
    """

    @pytest.mark.parametrize(
        "method",
        [
            pytest.param("eth_sendRawTransaction", id="evm broadcast"),
            pytest.param("parity_setMinGasPrice", id="evm parity setter"),
            pytest.param("debug_setHead", id="evm rewind"),
            pytest.param("engine_forkchoiceUpdatedV3", id="consensus api"),
            pytest.param("eth_submitWork", id="evm submit"),
            pytest.param("sendTransaction", id="solana broadcast"),
            pytest.param("requestAirdrop", id="solana airdrop"),
            pytest.param("sui_executeTransactionBlock", id="sui execute"),
            pytest.param("unsafe_moveCall", id="sui tx builder"),
            pytest.param("wallet/broadcasttransaction", id="tron broadcast"),
        ],
    )
    def test_mutating_methods_are_blocked(self, method: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(method)

    @pytest.mark.parametrize(
        "method",
        [
            pytest.param("eth_getLogs", id="evm logs"),
            pytest.param("getBalance", id="solana balance"),
            pytest.param("getSignaturesForAddress", id="solana history"),
            pytest.param("sui_dryRunTransactionBlock", id="sui dry run"),
            pytest.param("sui_devInspectTransactionBlock", id="sui inspect"),
            pytest.param("wallet/getaccount", id="tron account"),
        ],
    )
    def test_reads_are_untouched(self, method: str) -> None:
        # The near-misses matter most: `sui_dryRun…` and `sui_devInspect…` sit
        # next to `sui_execute…` and are reads.
        assert_read_only(method)

    def test_it_is_a_deny_list_on_purpose(self) -> None:
        """An allow-list would refuse every read nobody enumerated.

        Solana, Sui and Tron read methods are bare names, and a plugin provider
        serving an unknown chain would be blocked at every call. A deny-list
        fails open on an unknown *read*, which is recoverable; an allow-list
        fails closed on all of them, which makes the library unusable by the
        people it exists to be extended by.
        """
        assert_read_only("somechain_getSomethingNobodyHasHeardOf")
