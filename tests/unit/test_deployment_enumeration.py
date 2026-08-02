"""Derived addresses, confirmed against the chain.

Derivation says what an address *would* be. It cannot say whether anything is
there, and a list of derived addresses presented as an actor's infrastructure
is a list of guesses.

Field notes call this the universal fallback for a chain with no explorer API:
BSC has no Blockscout instance and BscScan wants a key, and CREATE derivation
plus `eth_getCode` needs neither.
"""

from __future__ import annotations

import pytest

pytest.importorskip("eth_utils", reason="needs chainscope[evm]")

from chainscope.analysis.multichain import confirm_deployments, create_address

DEPLOYER = "0x0629b1048298AE9deff0F4100A31967Fb3f98962"


def chain_with(*live_nonces, fails=()):
    """A `code_at` where only the given nonces have code."""
    live = {create_address(DEPLOYER, n).lower() for n in live_nonces}
    broken = {create_address(DEPLOYER, n).lower() for n in fails}

    def code_at(address: str) -> str:
        if address.lower() in broken:
            raise RuntimeError("node timed out")
        return "0x6080604052" if address.lower() in live else "0x"

    return code_at


class TestItConfirmsWhatExists:
    def test_only_live_addresses_come_back(self):
        found, _ = confirm_deployments(DEPLOYER, chain_with(0, 3), count=6)
        assert [d.nonce for d in found] == [0, 3]

    def test_an_empty_account_is_not_a_deployment(self):
        found, _ = confirm_deployments(DEPLOYER, chain_with(), count=4)
        assert found == []

    def test_the_addresses_are_the_derived_ones(self):
        found, _ = confirm_deployments(DEPLOYER, chain_with(2), count=4)
        assert found[0].address == create_address(DEPLOYER, 2)

    @pytest.mark.parametrize("empty", ["0x", "", "0x0", "0x00"])
    def test_every_spelling_of_no_code_is_refused(self, empty):
        found, _ = confirm_deployments(DEPLOYER, lambda a: empty, count=3)
        assert found == []

    def test_known_addresses_are_marked(self):
        known = {create_address(DEPLOYER, 1).lower()}
        found, _ = confirm_deployments(DEPLOYER, chain_with(0, 1), count=3, known=known)
        assert [d.is_known for d in found] == [False, True]


class TestAFailureIsNotAnAbsence:
    """A provider failing on nonce 7 is not evidence that nothing was deployed
    at nonce 7. Folding the two together reports a hole in the data as an
    absence of infrastructure."""

    def test_failures_are_reported_separately(self):
        found, unchecked = confirm_deployments(DEPLOYER, chain_with(0, fails=(1, 2)), count=3)
        assert [d.nonce for d in found] == [0]
        assert len(unchecked) == 2

    def test_the_reason_survives(self):
        _, unchecked = confirm_deployments(DEPLOYER, chain_with(fails=(0,)), count=1)
        assert "timed out" in unchecked[0]

    def test_the_nonce_survives(self):
        _, unchecked = confirm_deployments(DEPLOYER, chain_with(fails=(5,)), count=6)
        assert "nonce 5" in unchecked[0]

    def test_a_failure_does_not_stop_the_walk(self):
        """One dead nonce in the middle must not truncate everything after."""
        found, _ = confirm_deployments(DEPLOYER, chain_with(0, 4, fails=(2,)), count=6)
        assert [d.nonce for d in found] == [0, 4]

    def test_an_error_string_is_not_mistaken_for_code(self):
        """A provider returning "rate limited" is truthy. Checking for
        meaningful hex rather than truthiness is what keeps that from being
        recorded as a contract."""
        found, _ = confirm_deployments(DEPLOYER, lambda a: "0x" + "0" * 64, count=2)
        assert found == []
