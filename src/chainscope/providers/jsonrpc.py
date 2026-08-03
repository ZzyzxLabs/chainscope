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

from ..core.chainid import ChainId, native_symbol
from ..core.models import Account, Address, Block, Transaction, TxRef
from ..core.units import Amount
from ..transport.cache import Volatility
from ..transport.http import Client, TransportError
from .base import Capability, CostTier, Provider, ProviderError, ReadOnlyProvider

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

    #: What any JSON-RPC endpoint offers. Declared on the class so that
    #: discovery --- `chainscope doctor`, the plugin loader, anything reading
    #: entry points --- can report what this provider does without constructing
    #: one against a live URL. The instance adds ARCHIVE_STATE and TRACE when
    #: the endpoint supports them, since those genuinely vary per node.
    ecosystems = frozenset({"eip155"})

    capabilities = _BASE_CAPS

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
        # The instance narrows or widens the class-level declaration, which
        # stays as the set every JSON-RPC endpoint offers. A class advertising
        # NONE is invisible to anything that inspects providers without
        # constructing them --- `chainscope doctor` listed this provider with no
        # capabilities at all.
        caps = _BASE_CAPS
        if archive:
            caps |= Capability.ARCHIVE_STATE
        if trace:
            caps |= Capability.TRACE
        self.capabilities = caps

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """The configured endpoint for this chain, if there is one.

        Endpoints are keyed by short name (``CHAINSCOPE_RPC_ETHEREUM``), so the
        lookup goes through the alias table rather than CAIP-2 --- a user types
        ``eth``, and ``eip155:1`` is not a thing anybody sets in a shell profile.

        Archive is **declared, never assumed**. Whether a node keeps historical
        state is a property of that node, not of the URL, and inferring it from
        a hostname would make the router pick an endpoint that then fails --- so
        it is off unless `CHAINSCOPE_RPC_<NAME>_ARCHIVE` says otherwise.

        It has to be declarable, though. Without it every historical `eth_call`
        and `eth_getCode` is refused, and those are what answer "was this an
        EOA at block b" and "what were this token's decimals then" --- questions
        whose present-tense answers are quietly different. Asking a live node
        for state two years old is exactly the confident wrong answer this
        package refuses elsewhere.
        """
        from ..core.chainid import ALIASES

        if not cls.serves(chain):
            return []
        names = [n for n, c in ALIASES.items() if c == chain]
        for name in sorted(names, key=len, reverse=True):
            url = settings.rpc.get(name)
            if url:
                return [
                    cls(
                        url,
                        chain,
                        client=client,
                        native_symbol=native_symbol(chain, "ETH"),
                        archive=settings.rpc_archive.get(name, False),
                    )
                ]
        return []

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

        # Verify the block that came back is the block that was asked for.
        #
        # Field notes from a real multi-chain trace record this failure costing
        # a submitted answer: a block number off by one hex digit returned a
        # different block, and the timestamps taken from it put an event days
        # from where it happened. Nothing errored. Every conclusion built on
        # that timestamp was confidently wrong.
        #
        # The conversion here cannot be mistyped, but the *response* can still
        # be the wrong one: a cache entry scoped too loosely, a JSON-RPC batch
        # whose ids got crossed -- the same notes warn about both. A response
        # carries its own number, so checking is one comparison, and the
        # alternative is a wrong timestamp that looks exactly like a right one.
        returned = _hexint(raw.get("number"))
        if not isinstance(number, str) and returned != number:
            raise ProviderError(
                f"asked for block {number} and the endpoint returned {returned}. "
                f"Refusing rather than using it: timestamps from the wrong block "
                f"are indistinguishable from correct ones downstream. This is "
                f"usually a mis-scoped cache entry or crossed JSON-RPC batch ids."
            )
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
        # A mined transaction with no receipt has an unknowable outcome, and
        # defaulting to True says it succeeded. `success` is a `bool`, so there
        # is no "unknown" to record --- and `None` would be falsy, turning
        # unknown into failed, which is the worse error of the two. So: refuse,
        # and let the router try another provider.
        #
        # A *pending* transaction has no receipt yet and no block either. That
        # is not an unknown outcome, it is an outcome that has not happened, and
        # the caller sees it as `block=None`.
        if block_no and not receipt:
            raise ProviderError(
                f"transaction {tx_hash} is in block {block_no} and its receipt "
                f"could not be read, so whether it succeeded is unknown. "
                f"Reporting it as successful would count a possible revert as a "
                f"movement."
            )
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

    def code_at(self, chain: ChainId, address: str, block: int | str = "latest") -> str:
        """``eth_getCode`` at a specific block. Historical needs ``ARCHIVE_STATE``.

        `get_account` asks at ``latest``, which answers a different question.
        Whether an address is a contract is not a fixed property: a
        counterfactual deployment turns an EOA into a contract, and a
        self-destruct turned one back. Asking "now" to decide what something was
        two years ago produces a confident wrong answer with nothing to notice,
        which is the failure mode this package exists to refuse.

        Returns the raw byte string. `"0x"` means no code *at that block*, which
        is what "EOA at block b" means and is a narrower claim than "not a
        contract" --- an address with no code yet may still receive one.
        """
        if block != "latest" and not self.capabilities & Capability.ARCHIVE_STATE:
            raise ProviderError(
                f"{self.name} is not configured as an archive node; "
                f"eth_getCode at block {block} needs archive state. "
                f"Pass archive=True if this endpoint does serve history."
            )
        tag = block if isinstance(block, str) else hex(block)
        vol = Volatility.LIVE if block == "latest" else Volatility.IMMUTABLE
        return self._call("eth_getCode", [address, tag], vol) or "0x"

    def nonce_at(self, chain: ChainId, address: str, block: int | str = "latest") -> int:
        """``eth_getTransactionCount`` at a block. Historical needs archive state.

        The companion to `code_at`, and needed for the same reason: "no code
        and no nonce at block b" is how a *dormant* address is identified, and
        both halves have to be asked as of that block. `get_account` asks at
        `latest`, which answers whether it is dormant now --- a different
        question, and one whose answer changes every time somebody spends from
        it.
        """
        if block != "latest" and not self.capabilities & Capability.ARCHIVE_STATE:
            raise ProviderError(
                f"{self.name} is not configured as an archive node; "
                f"eth_getTransactionCount at block {block} needs archive state"
            )
        tag = block if isinstance(block, str) else hex(block)
        vol = Volatility.LIVE if block == "latest" else Volatility.IMMUTABLE
        return _hexint(self._call("eth_getTransactionCount", [address, tag], vol) or "0x0")

    def is_eoa_at(self, chain: ChainId, address: str, block: int | str = "latest") -> bool:
        """Whether ``address`` had no code at ``block``."""
        return self.code_at(chain, address, block) == "0x"

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
        # From 0, not 1. The genesis block is a block, and starting at 1 made
        # every moment between genesis and the first block unanswerable ---
        # reported as "before the chain existed" when the chain plainly did.
        low, high = 0, self.block_number()
        # `None`, not 1. Seeded with the lowest block, a timestamp *before* the
        # chain existed never updated it and the search returned block 1 ---
        # timestamped after the moment asked about, which is precisely the
        # failure this docstring says it exists to prevent. Measured: asking
        # for 2010-01-01 on Ethereum returned block 1, July 2015.
        best: int | None = None
        while low <= high:
            mid = (low + high) // 2
            # A provider failure is not evidence about which half to search.
            # Treating it as "mid is too late" silently biased the search
            # downward, so a flaky endpoint produced a wrong block rather than
            # an error --- and the wrong block here is the whole answer.
            block = self.get_block(chain, mid)
            if int(block.timestamp.timestamp()) <= target:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        if best is None:
            raise ProviderError(
                f"{when.isoformat()} is before the first block on {chain}. "
                f"There is no state as of that moment, and returning the "
                f"earliest block would answer with a time after the one asked "
                f"about."
            )
        return self.get_block(chain, best)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url
