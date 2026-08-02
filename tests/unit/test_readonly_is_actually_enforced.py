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

from chainscope.transport.http import ReadOnlyViolation, assert_payload_read_only

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
