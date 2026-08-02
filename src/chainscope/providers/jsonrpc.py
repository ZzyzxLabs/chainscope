"""Generic EVM JSON-RPC provider.

Works against any ``eth_*`` endpoint: a public node, your own geth, or a paid
archive service. What it cannot do is list an address's transaction history ---
no RPC method exists for that, which is why explorer-class providers exist and
why :class:`Capability` is modelled the way it is.

``ARCHIVE_STATE`` is not declared by default. Whether an endpoint serves
historical state is a property of that deployment, not of the protocol, so it
has to be stated by whoever configures it. Claiming it falsely would make the
router prefer a node that then returns "missing trie node" for every historical
query.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.chainid import ChainId
from ..core.models import Account, Address, Block, Transaction, TxRef
from ..core.units import Amount
from ..transport.cache import Volatility
from ..transport.http import Client, TransportError
from .base import Capability, CostTier, ProviderError, ReadOnlyProvider

__all__ = ["JsonRpcProvider"]

_BASE_CAPS = (
    Capability.BLOCK
    | Capability.TRANSACTION
    | Capability.RECEIPT
    | Capability.LOGS
    | Capability.BALANCE
)


def _hexint(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(value, 16)


class JsonRpcProvider(ReadOnlyProvider):
    """EVM JSON-RPC over HTTP."""

    def __init__(
        self,
        url: str,
        chain: ChainId,
        *,
        name: str | None = None,
        client: Client | None = None,
        archive: bool = False,
        trace: bool = False,
        cost: CostTier = CostTier.FREE_PUBLIC,
        native_symbol: str = "ETH",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(client)
        self.url = url
        self.chains = frozenset({chain})
        self.name = name or _host(url)
        self.cost = cost
        self.native_symbol = native_symbol
        self.headers = headers or {}
        caps = _BASE_CAPS
        if archive:
            caps |= Capability.ARCHIVE_STATE
        if trace:
            caps |= Capability.TRACE
        self.capabilities = caps

    @property
    def endpoint(self) -> str:
        return self.url

    # ---------------------------------------------------------------- helpers

    def _call(
        self,
        method: str,
        params: list[Any] | None = None,
        volatility: Volatility = Volatility.IMMUTABLE,
    ) -> Any:
        try:
            return self.client.rpc(
                self.url,
                method,
                params,
                volatility=volatility,
                headers=self.headers,
                provider=self.name,
                # Cache by chain, not by endpoint: the answer belongs to the
                # chain, and two endpoints for different chains can share a host.
                scope=str(self._chain()),
            )
        except TransportError as exc:
            raise ProviderError(str(exc)) from exc

    def _chain(self) -> ChainId:
        return next(iter(self.chains))

    def _addr(self, raw: str | None) -> Address | None:
        if not raw:
            return None
        return Address(self._chain(), raw, raw.lower())

    def _native(self, raw: Any) -> Amount:
        return Amount(_hexint(raw), 18, self.native_symbol)

    # ---------------------------------------------------------------- queries

    def healthy(self) -> bool:
        try:
            self._call("eth_blockNumber", [], Volatility.HEAD)
            return True
        except ProviderError:
            return False

    def block_number(self) -> int:
        return _hexint(self._call("eth_blockNumber", [], Volatility.HEAD))

    def get_block(self, chain: ChainId, number: int | str) -> Block:
        tag = number if isinstance(number, str) else hex(number)
        # "latest" is a moving target; a numbered block is finalised history.
        vol = Volatility.HEAD if isinstance(number, str) else Volatility.IMMUTABLE
        raw = self._call("eth_getBlockByNumber", [tag, False], vol)
        if not raw:
            raise ProviderError(f"block {number} not found")
        return Block(
            chain=chain,
            number=_hexint(raw["number"]),
            hash=raw["hash"],
            timestamp=datetime.fromtimestamp(_hexint(raw["timestamp"]), tz=timezone.utc),
            tx_count=len(raw.get("transactions", [])),
            parent=raw.get("parentHash"),
        )

    def get_transaction(self, chain: ChainId, tx_hash: str) -> Transaction:
        raw = self._call("eth_getTransactionByHash", [tx_hash])
        if not raw:
            raise ProviderError(f"transaction {tx_hash} not found")
        receipt = self._call("eth_getTransactionReceipt", [tx_hash]) or {}
        block_no = _hexint(raw.get("blockNumber"))
        timestamp: datetime | None = None
        if block_no:
            timestamp = self.get_block(chain, block_no).timestamp
        gas_used = _hexint(receipt.get("gasUsed"))
        gas_price = _hexint(raw.get("effectiveGasPrice") or raw.get("gasPrice"))
        return Transaction(
            ref=TxRef(chain, tx_hash.lower()),
            sender=self._addr(raw.get("from")),
            recipient=self._addr(raw.get("to")),
            value=self._native(raw.get("value")),
            timestamp=timestamp,
            block=block_no or None,
            success=_hexint(receipt.get("status")) == 1 if receipt else True,
            fee=Amount(gas_used * gas_price, 18, self.native_symbol),
            nonce=_hexint(raw.get("nonce")),
            input_data=raw.get("input", ""),
        )

    def get_account(self, chain: ChainId, address: str) -> Account:
        balance = self._call("eth_getBalance", [address, "latest"], Volatility.LIVE)
        code = self._call("eth_getCode", [address, "latest"], Volatility.SLOW)
        nonce = self._call("eth_getTransactionCount", [address, "latest"], Volatility.LIVE)
        return Account(
            address=Address(chain, address, address.lower()),
            balance=self._native(balance),
            tx_count=_hexint(nonce),
            is_contract=bool(code and code != "0x"),
        )

    def get_logs(
        self,
        chain: ChainId,
        *,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
    ) -> list[dict[str, Any]]:
        f: dict[str, Any] = {
            "fromBlock": from_block if isinstance(from_block, str) else hex(from_block),
            "toBlock": to_block if isinstance(to_block, str) else hex(to_block),
        }
        if address:
            f["address"] = address
        if topics:
            f["topics"] = topics
        vol = Volatility.SETTLED if to_block != "latest" else Volatility.LIVE
        return self._call("eth_getLogs", [f], vol) or []

    def call(self, chain: ChainId, to: str, data: str, block: int | str = "latest") -> str:
        """``eth_call``. Historical blocks need ``ARCHIVE_STATE``."""
        if block != "latest" and not self.capabilities & Capability.ARCHIVE_STATE:
            raise ProviderError(
                f"{self.name} is not configured as an archive node; "
                f"historical eth_call at block {block} will likely fail. "
                f"Pass archive=True if this endpoint does serve history."
            )
        tag = block if isinstance(block, str) else hex(block)
        vol = Volatility.LIVE if block == "latest" else Volatility.IMMUTABLE
        return self._call("eth_call", [{"to": to, "data": data}, tag], vol) or "0x"

    def block_at_time(self, chain: ChainId, when: datetime) -> Block:
        """Last block at or before ``when``, by binary search.

        Needed because analyses are anchored to a moment ("state as of the
        incident"), while chains are indexed by height. Doing this by hand is
        error-prone in a specific way: it is easy to return the block *after*
        the timestamp and quietly include a transaction that had not happened
        yet.
        """
        if when.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        target = int(when.timestamp())
        low, high = 1, self.block_number()
        best = low
        while low <= high:
            mid = (low + high) // 2
            try:
                block = self.get_block(chain, mid)
            except ProviderError:
                high = mid - 1
                continue
            if int(block.timestamp.timestamp()) <= target:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        return self.get_block(chain, best)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url
