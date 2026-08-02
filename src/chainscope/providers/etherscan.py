"""Etherscan-family explorer provider.

The capability that makes this worth writing is ``ADDRESS_HISTORY``. No JSON-RPC
method lists an address's transactions --- ``eth_getLogs`` finds events, not
sends --- so without an explorer-class provider, the question "what did this
address do" has no answer, and every traversal built on it is unreachable.

One API key covers sixty-odd EVM chains through Etherscan's V2 endpoint, which
takes ``chainid`` as a parameter. That is why this file is not
``etherscan.py``/``bscscan.py``/``polygonscan.py``: they are one service now.

**Three response shapes to know about**, because each has caused a wrong
analysis somewhere:

* ``status: "0"`` with ``"No transactions found"`` means *no data*. It is not
  an error, and treating it as one loses real empty results.
* ``status: "0"`` with a rate-limit message means *we do not know*. Returning
  an empty list here is the failure this whole design exists to prevent: an
  address silently vanishes from the analysis.
* A successful response is capped. Etherscan returns at most 10,000 records per
  query regardless of what you ask for, so a busy address is truncated by the
  API before pagination is even considered --- and it says so nowhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.chainid import ChainId, native_symbol
from ..core.models import (
    Account,
    Address,
    Transaction,
    Transfer,
    TransferKind,
    TxRef,
)
from ..core.units import Amount
from ..transport.cache import Volatility
from ..transport.http import Client, TransportError
from .base import (
    Capability,
    CostTier,
    Provider,
    ProviderError,
    ReadOnlyProvider,
    ResultTruncated,
)

__all__ = ["ETHERSCAN_V2", "EtherscanProvider", "ResultTruncated", "is_cacheable"]

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"

#: Hard cap the API applies per query, whatever ``offset`` asks for.
MAX_RECORDS = 10_000

_NO_DATA = ("no transactions found", "no records found", "no internal transactions found")

#: Stand-in for "the chain tip". Etherscan documents `endblock` as an integer,
#: so "latest" cannot be passed through. The common idiom is 99,999,999 --- but
#: BSC produces a block every three seconds and would reach that around 2030,
#: at which point queries would silently start missing recent history. A billion
#: buys roughly a century even at that rate.
END_OF_CHAIN = 999_999_999


def _end_block(value: int | str) -> int:
    return END_OF_CHAIN if value == "latest" else int(value)


def is_cacheable(body: Any) -> bool:
    """Whether a response is an answer rather than a refusal.

    Etherscan reports a rate limit as ``200 OK`` carrying ``status: "0"``. The
    transport sees a valid response and stores it, so a momentary rate limit
    becomes the cached answer for that address --- for an hour under the default
    ``SLOW`` policy, and permanently in a recorded cassette. Re-running does not
    help, because re-running does not make a request.

    "No transactions found" shares the same ``status: "0"`` and *is* an answer,
    so the two must be separated here rather than by status alone.
    """
    if not isinstance(body, dict):
        return False
    if "status" not in body:
        # A proxy-module (JSON-RPC) reply. Cache it unless it carries an error.
        return "error" not in body
    if body.get("status") == "1":
        return True
    return any(n in str(body.get("message", "")).lower() for n in _NO_DATA)


def _hex(value: Any) -> int | None:
    """A hex-or-decimal quantity from a proxy response, or ``None``."""
    if value in (None, "", "0x"):
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _row_index(action: str, row: dict[str, Any]) -> int:
    """What separates two movements inside one transaction.

    Token rows carry `logIndex`, which is the real discriminator. `txlist` and
    `txlistinternal` produce no log, so `transactionIndex` is used --- it does
    not separate two internal calls within one transaction, and nothing in the
    API does. That residue is a known limit rather than a silent one: the
    remaining collision is two internal transfers of an identical amount
    between the same pair in the same transaction.
    """
    field = "logIndex" if action == "tokentx" else "transactionIndex"
    raw = row.get(field)
    try:
        return int(str(raw), 0) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


class EtherscanProvider(ReadOnlyProvider):
    """Explorer-backed history for EVM chains."""

    name = "etherscan"
    ecosystems = frozenset({"eip155"})
    capabilities = (
        Capability.ADDRESS_HISTORY
        | Capability.ASSET_TRANSFERS
        | Capability.TRANSACTION
        | Capability.BALANCE
        | Capability.CONTRACT_SOURCE
    )
    cost = CostTier.FREE_KEYED

    def __init__(
        self,
        api_key: str,
        chains: frozenset[ChainId] | None = None,
        *,
        client: Client | None = None,
        base_url: str = ETHERSCAN_V2,
        native_symbol: str = "ETH",
    ) -> None:
        super().__init__(client)
        if not api_key:
            raise ValueError(
                "Etherscan V2 needs an API key. It is free, and one key covers "
                "every supported EVM chain."
            )
        self.api_key = api_key
        self.base_url = base_url
        self.native_symbol = native_symbol
        self.chains = chains or frozenset({ChainId.evm(1)})

    @property
    def endpoint(self) -> str:
        return self.base_url

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """One instance, scoped to the chain asked for.

        Etherscan V2 serves every supported EVM chain from one key, so the
        instance is built for whichever chain the caller wants rather than a
        fixed default --- ``chains={eth}`` on a BSC query is the exact shape of
        misconfiguration that makes a router report "no provider" while the
        credential sits right there.
        """
        if not cls.serves(chain):
            return []
        secret = settings.credentials.get(cls.name)
        # `Secret.__bool__` is the emptiness check; `reveal()` is deliberately
        # the only way out, so the call site is visible in a grep for it.
        if not secret:
            return []
        return [
            cls(
                secret.reveal(),
                frozenset({chain}),
                client=client,
                # EVM chains share one adapter and it declares ETH, which is
                # right for exactly one of them. A BSC native transfer came back
                # denominated in ETH: correct number, wrong unit, and the number
                # reads fine.
                native_symbol=native_symbol(chain, "ETH"),
            )
        ]

    # ---------------------------------------------------------------- request

    def _get(
        self,
        chain: ChainId,
        module: str,
        action: str,
        *,
        volatility: Volatility = Volatility.SLOW,
        **params: Any,
    ) -> Any:
        chain_id = chain.evm_chain_id
        if chain_id is None:
            raise ProviderError(f"{chain} is not an EVM chain")
        try:
            body = self.client.get(
                self.base_url,
                {
                    "chainid": chain_id,
                    "module": module,
                    "action": action,
                    "apikey": self.api_key,
                    **params,
                },
                volatility=volatility,
                provider=self.name,
                cacheable=is_cacheable,
            )
        except TransportError as exc:
            raise ProviderError(str(exc)) from exc

        if not isinstance(body, dict):
            raise ProviderError(f"unexpected response shape: {type(body).__name__}")

        message = str(body.get("message", "")).lower()
        detail = str(body.get("result", ""))

        # The failure envelope is checked before the proxy short-circuit,
        # because Etherscan wraps *every* module in it -- `proxy` included.
        # Taking the proxy path first means a rate-limited eth_getCode returns
        # the error text as its result, and `bool(code)` then reports a
        # rate-limited EOA as a contract. The ValueError from a caller parsing
        # it as hex is the lucky outcome; that silent misclassification is not.
        if body.get("status") == "0" and "message" in body:
            # An empty result is a fact about the chain, not a failure. Only the
            # data modules can say it; proxy has no such response.
            if module != "proxy" and any(n in message for n in _NO_DATA):
                return []
            # Everything else is "we do not know". Raising rather than returning
            # [] is the point: an empty list here makes an address disappear
            # from the analysis with nothing to indicate it ever existed.
            raise ProviderError(f"{action}: {body.get('message')} ({detail})".strip())

        # The proxy module speaks JSON-RPC and has no `status` field at all.
        # Treating its absence as failure would break every nonce lookup.
        if module == "proxy":
            if "error" in body:
                raise ProviderError(f"{action}: {body['error']}")
            return body.get("result")

        if body.get("status") == "1":
            return body.get("result", [])

        raise ProviderError(f"{action}: {body.get('message')} ({detail})".strip())

    def _paged(
        self, chain: ChainId, module: str, action: str, *, limit: int, **params: Any
    ) -> list[dict[str, Any]]:
        # Compare against what was actually requested, not just the API cap. A
        # caller asking for 1000 and receiving exactly 1000 is truncated too,
        # and checking only MAX_RECORDS lets that pass silently -- which is the
        # failure mode this class exists to make impossible.
        effective = min(limit, MAX_RECORDS)
        rows = self._get(
            chain,
            module,
            action,
            page=1,
            offset=effective,
            sort="asc",
            **params,
        )
        if not isinstance(rows, list):
            raise ProviderError(f"{action}: expected a list of records")
        if len(rows) >= effective:
            cap = " (the API maximum)" if effective >= MAX_RECORDS else ""
            raise ResultTruncated(
                f"{action} returned {effective} records, exactly the number "
                f"requested{cap}. There is almost certainly more. Narrow the "
                f"block range and query again; any total from this set is a "
                f"lower bound."
            )
        return rows

    # ---------------------------------------------------------------- helpers

    def _addr(self, chain: ChainId, raw: str | None) -> Address | None:
        if not raw:
            return None
        return Address(chain, raw, raw.lower())

    @staticmethod
    def _when(row: dict[str, Any]) -> datetime | None:
        ts = row.get("timeStamp") or row.get("timestamp")
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None

    # ---------------------------------------------------------------- queries

    def address_history(
        self,
        chain: ChainId,
        address: str,
        *,
        start_block: int = 0,
        end_block: int | str = "latest",
        limit: int = 1000,
    ) -> list[Transaction]:
        """Every ordinary transaction an address sent or received."""
        rows = self._paged(
            chain,
            "account",
            "txlist",
            limit=limit,
            address=address,
            startblock=start_block,
            endblock=_end_block(end_block),
        )
        return [
            Transaction(
                ref=TxRef(chain, r["hash"].lower()),
                sender=self._addr(chain, r.get("from")),
                recipient=self._addr(chain, r.get("to")),
                value=Amount(int(r.get("value", 0)), 18, self.native_symbol),
                timestamp=self._when(r),
                block=int(r["blockNumber"]) if r.get("blockNumber") else None,
                # isError is "1" on revert. A reverted transaction moved no
                # value, and counting it inflates every total downstream.
                success=r.get("isError", "0") != "1",
                nonce=int(r["nonce"]) if r.get("nonce") else None,
                input_data=r.get("input", ""),
            )
            for r in rows
        ]

    def asset_transfers(
        self,
        chain: ChainId,
        address: str,
        *,
        direction: str = "out",
        start_block: int | str = 0,
        end_block: int | str = "latest",
        contract: str | None = None,
        limit: int = 1000,
    ) -> list[Transfer]:
        """Native, token, and internal transfers touching an address.

        Internal transfers are included deliberately. They produce no log and no
        top-level transaction, so a tracer that reads only the other two misses
        them --- and they are exactly where swap proceeds and withdrawal payouts
        live.
        """
        end = _end_block(end_block)
        start = 0 if start_block == "latest" else int(start_block)
        out: list[Transfer] = []

        token_params: dict[str, Any] = {}
        if contract:
            token_params["contractaddress"] = contract

        for action, kind in (
            ("txlist", TransferKind.NATIVE),
            ("txlistinternal", TransferKind.INTERNAL),
            ("tokentx", TransferKind.TOKEN),
        ):
            params = token_params if action == "tokentx" else {}
            try:
                rows = self._paged(
                    chain,
                    "account",
                    action,
                    limit=limit,
                    address=address,
                    startblock=start,
                    endblock=end,
                    **params,
                )
            except ResultTruncated:
                raise
            except ProviderError:
                # One endpoint failing should not lose the other two, but the
                # gap is real; callers see it as a shorter list. Analyzers that
                # need completeness use the nonce check instead.
                continue

            for r in rows:
                # Guarded, as the Blockscout provider already does. A row with
                # an empty or missing `value` raised `ValueError`, which is not
                # a `ProviderError` --- so `Router.dispatch` could not fall back
                # and one malformed row aborted the whole call. A provider
                # returning junk is exactly the case fallback exists for.
                value = _hex(r.get("value"))
                if not value:
                    continue
                if action == "txlist" and r.get("isError", "0") == "1":
                    continue
                # A token whose decimals will not parse is reported in base
                # units rather than assumed to be eighteen: a six-decimal
                # balance shown at eighteen is a trillion times too small,
                # which reads as dust and gets skipped.
                decimals = _hex(r.get("tokenDecimal"))
                decimals = 18 if decimals is None and action != "tokentx" else (decimals or 0)
                symbol = r.get("tokenSymbol") or self.native_symbol
                out.append(
                    Transfer(
                        chain=chain,
                        tx=TxRef(chain, r["hash"].lower()),
                        sender=self._addr(chain, r.get("from")),
                        recipient=self._addr(chain, r.get("to")),
                        amount=Amount(value, decimals, symbol),
                        kind=kind,
                        timestamp=self._when(r),
                        block=_hex(r.get("blockNumber")),
                        asset=self._addr(chain, r.get("contractAddress")),
                        # What distinguishes two movements in one transaction.
                        #
                        # Left at the default 0, so the store's uniqueness key
                        # --- which carries `log_index` precisely to keep these
                        # apart --- could not do its job: two identical token
                        # transfers in one transaction, an ordinary batched
                        # payout, collapsed to one row with no error. Measured:
                        # two rows in, one row out, the same shape the `asset`
                        # and `kind` fixes were written for.
                        #
                        # `logIndex` for token rows; `transactionIndex` for
                        # `txlist` and `txlistinternal`, which have no log.
                        index=_row_index(action, r),
                    )
                )

        key = address.lower()
        if direction == "out":
            out = [t for t in out if t.sender and t.sender.key == key]
        elif direction == "in":
            out = [t for t in out if t.recipient and t.recipient.key == key]
        out.sort(key=lambda t: (t.block or 0, t.index))
        return out

    def get_account(self, chain: ChainId, address: str) -> Account:
        """Balance and nonce.

        The nonce matters more than the balance. It equals the number of
        outbound transactions, which is the only cheap proof that a paginated
        history was not silently truncated --- and without it,
        `Account.completeness_check` returns None and analyzers lose their one
        guard against running on partial data.

        Etherscan does not expose the nonce through the account module, so this
        goes through the proxy module, which forwards a real RPC call.
        """
        balance = self._get(
            chain,
            "account",
            "balance",
            volatility=Volatility.LIVE,
            address=address,
            tag="latest",
        )
        nonce_hex = self._get(
            chain,
            "proxy",
            "eth_getTransactionCount",
            volatility=Volatility.LIVE,
            address=address,
            tag="latest",
        )
        code = self._get(
            chain,
            "proxy",
            "eth_getCode",
            volatility=Volatility.SLOW,
            address=address,
            tag="latest",
        )
        return Account(
            address=Address(chain, address, address.lower()),
            balance=Amount(int(balance or 0), 18, self.native_symbol),
            tx_count=int(str(nonce_hex), 16) if nonce_hex else None,
            is_contract=bool(code and code not in ("0x", "")),
        )

    def get_transaction(self, chain: ChainId, tx_hash: str) -> Transaction:
        """One transaction by hash, through the proxy module.

        The class declared `Capability.TRANSACTION` and inherited the base
        implementation, which refuses --- so the router, which reads the
        declaration, picked the primary EVM provider for every transaction
        lookup and got "etherscan does not provide transactions". The mixer
        resolves its deposit hashes through this capability; the same
        declaration-without-implementation was fixed in the Sui and Blockscout
        providers already, and this is the third.

        `Capability`'s own docstring: "Overstating is worse than omitting: the
        router will select you, the call returns partial data, and an analyzer
        draws a conclusion from an incomplete picture."

        The receipt is fetched too, because `success` cannot be read from the
        transaction alone --- and a failed transaction counted as a movement is
        how a trace follows money that never went anywhere.
        """
        raw = self._get(
            chain,
            "proxy",
            "eth_getTransactionByHash",
            volatility=Volatility.SETTLED,
            txhash=tx_hash,
        )
        if not isinstance(raw, dict) or not raw.get("hash"):
            raise ProviderError(f"no transaction {tx_hash} on {chain}")

        receipt = self._get(
            chain,
            "proxy",
            "eth_getTransactionReceipt",
            volatility=Volatility.SETTLED,
            txhash=tx_hash,
        )
        receipt = receipt if isinstance(receipt, dict) else {}

        gas_used = _hex(receipt.get("gasUsed"))
        gas_price = _hex(raw.get("gasPrice"))
        block = _hex(raw.get("blockNumber"))
        return Transaction(
            ref=TxRef(chain, str(raw["hash"]).lower()),
            sender=self._addr(chain, raw.get("from")),
            recipient=self._addr(chain, raw.get("to")),
            value=Amount(_hex(raw.get("value")) or 0, 18, self.native_symbol),
            # Not available from this endpoint. Left as None rather than filled
            # with "now", which would date a settled transaction to the moment
            # it was looked up.
            timestamp=None,
            block=block or None,
            success=_hex(receipt.get("status")) == 1 if receipt else True,
            fee=Amount((gas_used or 0) * (gas_price or 0), 18, self.native_symbol),
            nonce=_hex(raw.get("nonce")),
            input_data=str(raw.get("input") or ""),
        )

    def contract_source(self, chain: ChainId, address: str) -> dict[str, Any]:
        rows = self._get(
            chain,
            "contract",
            "getsourcecode",
            volatility=Volatility.SETTLED,
            address=address,
        )
        return rows[0] if isinstance(rows, list) and rows else {}
