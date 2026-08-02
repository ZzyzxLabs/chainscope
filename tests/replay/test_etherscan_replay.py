"""The Etherscan provider against real recorded responses.

Everything here replays ``tests/cassettes/etherscan_mainnet.json``, recorded
once from the live API by ``scripts/record_cassettes.py``. Sockets are blocked
for this suite, so a miss fails rather than quietly reaching the network.

These tests do something the unit tests structurally cannot. A hand-written
fake returns whatever its author believed the API returns; if the belief was
wrong, the fake and the code agree with each other and both are wrong. Only
recorded traffic can disagree.

The API key used here is a placeholder, and that is a load-bearing detail: cache
keys carry no credential, so a recording made with one key replays under any
key or none. If that ever regresses, every test in this file fails at once ---
which is the alarm it is meant to be.
"""

from pathlib import Path

import pytest

from chainscope.core.chainid import ETHEREUM
from chainscope.providers.etherscan import EtherscanProvider, ResultTruncated
from chainscope.transport.cassette import Cassette
from chainscope.transport.http import Client

CASSETTE = Path(__file__).resolve().parents[1] / "cassettes" / "etherscan_mainnet.json"

RONIN_EXPLOITER = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
RONIN_FROM, RONIN_TO = 14_442_000, 14_460_000
TORNADO_100 = "0xA160cdAB225685dA1d56aa342Ad8841c3b53f291"
BINANCE_14 = "0x28C6c06298d514Db089934071355E5743bf21d60"
BINANCE_FROM, BINANCE_TO = 21_000_000, 21_000_030
UNUSED = "0x0000000000000000000000000000000000031337"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

pytestmark = pytest.mark.skipif(
    not CASSETTE.is_file(),
    reason=f"{CASSETTE.name} not recorded; run scripts/record_cassettes.py",
)


@pytest.fixture
def provider():
    # A placeholder key on purpose -- see the module docstring.
    return EtherscanProvider("not-a-real-key", client=Client(cache=Cassette(CASSETTE)))


class TestReplayIsCredentialFree:
    def test_the_cassette_carries_no_credential(self):
        from chainscope.config import Settings
        from chainscope.transport.cassette import assert_no_credentials

        Settings.load()  # registers whatever this machine has configured
        assert_no_credentials(CASSETTE)

    def test_any_key_replays_the_same_recording(self):
        """Two providers, two different keys, one recording."""
        a = EtherscanProvider("key-one", client=Client(cache=Cassette(CASSETTE)))
        b = EtherscanProvider("key-two", client=Client(cache=Cassette(CASSETTE)))
        assert len(
            a.address_history(
                ETHEREUM, RONIN_EXPLOITER, start_block=RONIN_FROM, end_block=RONIN_TO, limit=100
            )
        ) == len(
            b.address_history(
                ETHEREUM, RONIN_EXPLOITER, start_block=RONIN_FROM, end_block=RONIN_TO, limit=100
            )
        )


class TestAddressHistory:
    def _history(self, provider):
        return provider.address_history(
            ETHEREUM, RONIN_EXPLOITER, start_block=RONIN_FROM, end_block=RONIN_TO, limit=100
        )

    def test_a_bounded_range_returns_a_complete_history(self, provider):
        """Under the requested limit, so nothing was cut off."""
        assert 0 < len(self._history(provider)) < 100

    def test_every_transaction_parses(self, provider):
        for tx in self._history(provider):
            assert tx.ref.hash.startswith("0x")
            assert tx.block is not None and RONIN_FROM <= tx.block <= RONIN_TO
            assert tx.timestamp is not None

    def test_amounts_are_exact_integers(self, provider):
        """Real wei values, not floats. 3390 ETH does not survive a double."""
        largest = max(self._history(provider), key=lambda t: t.value.raw)
        assert largest.value.raw > 10**21
        assert isinstance(largest.value.raw, int)

    def test_ordering_is_by_block(self, provider):
        blocks = [t.block for t in self._history(provider)]
        assert blocks == sorted(blocks)

    def test_an_address_with_no_history_returns_empty(self, provider):
        """'No transactions found' is a fact about the chain, and shares its
        `status: "0"` with rate limiting -- which is not."""
        assert provider.address_history(ETHEREUM, UNUSED, limit=10) == []

    def test_a_busy_address_reports_truncation(self, provider):
        """Recorded from the real API: five rows requested, five returned."""
        with pytest.raises(ResultTruncated, match="lower bound"):
            provider.address_history(ETHEREUM, BINANCE_14, limit=5)


class TestTransfers:
    def _transfers(self, provider):
        return provider.asset_transfers(
            ETHEREUM,
            BINANCE_14,
            direction="all",
            contract=USDC,
            start_block=BINANCE_FROM,
            end_block=BINANCE_TO,
            limit=100,
        )

    def test_token_decimals_come_from_the_response(self, provider):
        """USDC is six decimals. Assuming eighteen is wrong by a factor of a
        trillion, and no hand-written fake would have caught it."""
        usdc = [t for t in self._transfers(provider) if t.amount.symbol == "USDC"]
        assert usdc, "cassette should contain USDC transfers"
        assert all(t.amount.decimals == 6 for t in usdc)

    def test_token_amounts_render_sanely(self, provider):
        """A six-decimal amount read as eighteen shows up as ~0.000000000001."""
        usdc = [t for t in self._transfers(provider) if t.amount.symbol == "USDC"]
        assert max(t.amount.raw for t in usdc) > 1_000_000

    def test_native_and_token_transfers_coexist(self, provider):
        kinds = {t.kind.value for t in self._transfers(provider)}
        assert {"native", "token"} <= kinds

    def test_internal_transfers_are_included(self, provider):
        """They produce no log and no top-level transaction, so a tracer reading
        only the other two misses swap proceeds and withdrawal payouts."""
        transfers = provider.asset_transfers(
            ETHEREUM,
            RONIN_EXPLOITER,
            direction="all",
            start_block=RONIN_FROM,
            end_block=RONIN_TO,
            limit=100,
        )
        assert transfers


class TestAccounts:
    def test_an_eoa_reports_a_nonce_and_is_not_a_contract(self, provider):
        """The nonce is the only cheap proof a paginated history was complete."""
        account = provider.get_account(ETHEREUM, RONIN_EXPLOITER)
        assert account.tx_count is not None and account.tx_count > 0
        assert not account.is_contract

    def test_a_contract_is_detected(self, provider):
        """Tornado Cash's pool. Under the old proxy-path bug a rate-limited
        lookup returned an error string here, and `bool(code)` called it a
        contract -- so this passing for the right reason matters."""
        account = provider.get_account(ETHEREUM, TORNADO_100)
        assert account.is_contract

    def test_balances_are_exact(self, provider):
        """247,000 ETH is 2.47e23 wei, far past what a float holds exactly and
        past SQLite's INTEGER range too."""
        account = provider.get_account(ETHEREUM, TORNADO_100)
        assert account.balance.raw > 10**23
        assert isinstance(account.balance.raw, int)
