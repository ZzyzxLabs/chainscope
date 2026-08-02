"""Sui `get_transaction`, and the capability it used to only claim.

The provider declared ``Capability.TRANSACTION`` and inherited the base method,
which refuses. The router read the declaration, chose this provider --- the only
Sui provider, so nothing to fall back to --- and produced "sui does not provide
transactions" at the point of use, after ``applicable()`` had already said yes.

The arithmetic below is the part worth pinning. Sui reports *net* balance
changes and gas comes out of the same balance, so the sender's fall is the
transfer plus gas while the recipient's gain is the transfer alone. Reading the
sender's side as the amount overstates every outbound transfer.
"""

import pytest

from chainscope.chains.sui import SUI_MAINNET
from chainscope.providers.base import ProviderError
from chainscope.providers.sui import SuiProvider

D = "0x" + "1" * 64
S = "0x" + "a" * 64
R = "0x" + "b" * 64
SUI = "0x2::sui::SUI"


def reply(**over):
    body = {
        "digest": D,
        "checkpoint": "42",
        "timestampMs": "1700000000000",
        "transaction": {"data": {"sender": S}},
        "effects": {
            "status": {"status": "success"},
            "gasUsed": {
                "computationCost": "1000000",
                "storageCost": "2000000",
                "storageRebate": "500000",
            },
        },
        "balanceChanges": [
            {"owner": {"AddressOwner": S}, "coinType": SUI, "amount": "-1002500000"},
            {"owner": {"AddressOwner": R}, "coinType": SUI, "amount": "1000000000"},
        ],
    }
    body.update(over)
    return body


class Stub(SuiProvider):
    def __init__(self, body):
        super().__init__()
        self._body = body

    def _rpc(self, method, params, **kw):
        return self._body


def test_it_returns_a_transaction():
    tx = Stub(reply()).get_transaction(SUI_MAINNET, D)
    assert tx.ref.hash == D
    assert tx.sender and tx.sender.key == S
    assert tx.success
    assert tx.block == 42


def test_gas_is_net_of_the_rebate():
    tx = Stub(reply()).get_transaction(SUI_MAINNET, D)
    assert tx.fee.raw == 1_000_000 + 2_000_000 - 500_000


def test_the_transfer_is_the_recipients_gain_not_the_senders_loss():
    """The sender's balance fell by the transfer plus gas."""
    tx = Stub(reply()).get_transaction(SUI_MAINNET, D)
    assert len(tx.transfers) == 1
    assert tx.transfers[0].amount.raw == 1_000_000_000
    assert tx.transfers[0].recipient.key == R


def test_value_excludes_gas():
    tx = Stub(reply()).get_transaction(SUI_MAINNET, D)
    assert tx.value.raw == 1_000_000_000


def test_a_failed_transaction_says_so():
    body = reply()
    body["effects"]["status"]["status"] = "failure"
    assert not Stub(body).get_transaction(SUI_MAINNET, D).success


def test_a_missing_status_is_not_success():
    body = reply()
    body["effects"].pop("status")
    assert not Stub(body).get_transaction(SUI_MAINNET, D).success


def test_a_missing_transaction_raises():
    with pytest.raises(ProviderError, match="not found"):
        Stub({}).get_transaction(SUI_MAINNET, D)


def test_nine_decimals_not_eighteen():
    tx = Stub(reply()).get_transaction(SUI_MAINNET, D)
    assert tx.value.decimals == 9
