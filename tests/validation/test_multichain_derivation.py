"""Contract address derivation, checked against a real documented incident.

Unlike the clustering and change-detection harnesses, this one has *actual*
ground truth rather than constructed scenarios: the UNC4736 / Radiant Capital
compromise of October 2024 is publicly documented, and the deployer, the
contract, and the rehearsal contract are all named in the incident reports.

That makes it the strongest check available here. The derivation is either
byte-exact or it is wrong, and the published addresses say which.

The reference values, from the public reporting:

    operator EOA   0x0629b1048298AE9deff0F4100A31967Fb3f98962
    nonce 0        0x3c2bc83dcd293cc8a23526a37aaeedd83ebd62de  (rehearsal)
    nonce 3        0x57ba8957ed2ff2e7AE38F4935451E81Ce1eEFbf5  (executed)

Both were deployed at the same addresses on four chains, which is what
derivation guarantees and why the *matching addresses* are not themselves the
finding. The shared deployer is.
"""

from __future__ import annotations

import pytest

from chainscope.analysis.multichain import (
    correlate,
    create2_address,
    create_address,
    deployments_for,
    recover_nonce,
)
from chainscope.core.attribution import Confidence
from chainscope.core.chainid import ARBITRUM, BASE, BSC, ETHEREUM

pytest.importorskip("eth_utils", reason="needs chainscope[evm]")

OPERATOR = "0x0629b1048298AE9deff0F4100A31967Fb3f98962"
REHEARSAL = "0x3c2bc83dcd293cc8a23526a37aaeedd83ebd62de"
EXECUTED = "0x57ba8957ed2ff2e7AE38F4935451E81Ce1eEFbf5"


class TestAgainstRealGroundTruth:
    def test_nonce_zero_is_the_rehearsal_contract(self):
        assert create_address(OPERATOR, 0).lower() == REHEARSAL.lower()

    def test_nonce_three_is_the_contract_that_executed(self):
        assert create_address(OPERATOR, 3).lower() == EXECUTED.lower()

    def test_the_nonce_is_recoverable_from_the_contract(self):
        """The direction that matters in an investigation: a contract is known,
        the deployer is known, and what else they built is not."""
        assert recover_nonce(OPERATOR, EXECUTED) == 3
        assert recover_nonce(OPERATOR, REHEARSAL) == 0

    def test_enumerating_forward_finds_the_rehearsal_from_the_executed_one(self):
        """The prediction case. An actor tests at a low nonce and executes
        later; the test contract is often still sitting there unexamined."""
        siblings = deployments_for(OPERATOR, count=5, known={EXECUTED})
        rehearsal = next(d for d in siblings if d.nonce == 0)
        assert rehearsal.address.lower() == REHEARSAL.lower()
        assert not rehearsal.is_known  # nobody had looked at it


class TestDerivationCorrectness:
    def test_nonce_zero_encodes_as_empty_not_as_a_zero_byte(self):
        """RLP encodes 0 as the empty string. Getting this wrong shifts every
        derived address by one deployment, silently."""
        # If 0 encoded as b"\\x00", nonce 0 and some other nonce would collide
        # with the wrong values; the ground-truth check above is what catches
        # it, and this states the reason.
        assert create_address(OPERATOR, 0) != create_address(OPERATOR, 1)
        assert create_address(OPERATOR, 0).lower() == REHEARSAL.lower()

    def test_addresses_are_returned_checksummed(self):
        derived = create_address(OPERATOR, 3)
        assert derived != derived.lower()
        assert derived == EXECUTED

    def test_a_negative_nonce_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            create_address(OPERATOR, -1)

    @pytest.mark.parametrize("bad", ["0xdeadbeef", "", "0x" + "a" * 39, "not an address"])
    def test_a_malformed_deployer_is_refused(self, bad):
        with pytest.raises(ValueError):
            create_address(bad, 0)

    def test_the_derivation_does_not_depend_on_a_chain(self):
        """Which is the entire basis for cross-chain correlation, and also the
        reason matching addresses prove nothing on their own."""
        import inspect

        source = inspect.signature(create_address)
        assert "chain" not in source.parameters


class TestCreate2:
    def test_it_derives_something_stable(self):
        a = create2_address("0x" + "11" * 20, "0x" + "22" * 32, "0x" + "33" * 32)
        b = create2_address("0x" + "11" * 20, "0x" + "22" * 32, "0x" + "33" * 32)
        assert a == b

    def test_a_different_salt_gives_a_different_address(self):
        a = create2_address("0x" + "11" * 20, "0x" + "22" * 32, "0x" + "33" * 32)
        b = create2_address("0x" + "11" * 20, "0x" + "44" * 32, "0x" + "33" * 32)
        assert a != b

    @pytest.mark.parametrize(
        ("salt", "code"),
        [("0x" + "22" * 31, "0x" + "33" * 32), ("0x" + "22" * 32, "0x" + "33" * 31)],
    )
    def test_wrong_sized_inputs_are_refused(self, salt, code):
        """A 31-byte salt silently left-padded would derive a real address that
        is simply not the right one."""
        with pytest.raises(ValueError, match="expected 32 bytes"):
            create2_address("0x" + "11" * 20, salt, code)


class TestWhatItRefusesToClaim:
    def test_presence_without_a_deployer_claims_nothing(self):
        """Every address exists on every EVM chain by construction. Finding one
        on six networks is the default state of the world."""
        presence = correlate(EXECUTED, chains=[ETHEREUM, BSC, ARBITRUM, BASE])
        assert not presence.is_deterministic
        assert presence.attribution(ETHEREUM) is None
        assert "establishes nothing" in presence.summary()

    def test_a_recovered_deployer_is_the_actual_evidence(self):
        presence = correlate(
            EXECUTED, chains=[ETHEREUM, BSC, ARBITRUM, BASE], deployer=OPERATOR
        )
        assert presence.is_deterministic
        assert presence.nonce == 3
        assert "shared deployer" in presence.summary()

    def test_the_claim_never_exceeds_medium(self):
        """Deriving an address proves who *can* have deployed it at that nonce.
        A key is not a person."""
        presence = correlate(EXECUTED, chains=[ETHEREUM], deployer=OPERATOR)
        claim = presence.attribution(ETHEREUM)
        assert claim is not None
        assert claim.confidence <= Confidence.MEDIUM

    def test_the_summary_warns_that_code_may_differ(self):
        """Two chains can hold different contracts at one address."""
        presence = correlate(EXECUTED, chains=[ETHEREUM], deployer=OPERATOR)
        assert "different code" in presence.summary()

    def test_a_wrong_deployer_recovers_nothing_rather_than_guessing(self):
        presence = correlate(EXECUTED, chains=[ETHEREUM], deployer="0x" + "ab" * 20)
        assert not presence.is_deterministic
        assert presence.attribution(ETHEREUM) is None

    def test_not_found_is_not_the_same_as_not_deployed(self):
        """A deployer with four hundred contracts reports None for one it
        certainly produced, and reading that as a negative dismisses a real
        link."""
        assert recover_nonce(OPERATOR, EXECUTED, search=2) is None
        assert recover_nonce(OPERATOR, EXECUTED, search=10) == 3
