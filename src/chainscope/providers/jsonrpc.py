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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from ..core.chainid import ChainId, native_symbol
from ..core.models import Account, Address, Block, Transaction, Transfer, TransferKind, TxRef
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

__all__ = ["JsonRpcProvider"]

_BASE_CAPS = (
    Capability.BLOCK
    | Capability.TRANSACTION
    | Capability.RECEIPT
    | Capability.LOGS
    | Capability.BALANCE
    # Every EVM endpoint serves eth_getLogs, so every one of them can enumerate
    # token movement. See `asset_transfers` for what that does and does not
    # cover, and `Capability.TOKEN_TRANSFERS` for why it is not ASSET_TRANSFERS.
    | Capability.TOKEN_TRANSFERS
)

#: ``keccak256("Transfer(address,address,uint256)")``.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: How many blocks to ask for at once, and how far down to back off.
#:
#: Public endpoints cap `eth_getLogs` by range, by result count, or by wall
#: clock, and they disagree about which and say so in incompatible ways. Rather
#: than carry a table of per-host limits that goes stale, the scan starts wide
#: and halves on refusal --- measured against four BSC endpoints, the working
#: span ranged from 1,000 to over 20,000 blocks for the same query.
#: Start conservative and stay there, rather than starting wide and narrowing.
#:
#: Narrowing costs a *failure* per step, and a failure that is a timeout is
#: indistinguishable from an unwell host --- so four concurrent chunks each
#: halving once spent the circuit breaker's five lives before converging, and
#: the fetch died reporting "circuit open" about a node that was answering.
#: Measured against one BSC endpoint on two different days: 20,000 blocks
#: returned in 7s and later timed out at 30s, while 5,000 held at ~10s
#: throughout. The wide value is only faster when it works.
_SPAN_START = 5_000
_SPAN_FLOOR = 500

#: How wide a chunk may grow once the endpoint has proved it can take it.
#:
#: Starting narrow and widening on *success* costs nothing when it fails to
#: widen, where starting wide and narrowing on failure costs a failed request
#: per step --- and a failed request that is a timeout is indistinguishable
#: from an unwell host, which is what opened the circuit breaker mid-fetch.
#: So the first chunk is cheap and cautious and the rest ride on what it
#: learned.
_SPAN_CEILING = 40_000

#: How far back a scan reaches when the caller does not say.
#:
#: Not genesis. A full-history scan is hundreds of thousands of requests, and
#: issuing it because somebody left a default alone is not a thing this should
#: do quietly. The window is stated in the truncation message whenever it does
#: not reach the requested start, so a short answer is never mistaken for a
#: complete one.
#: How far back a scan reaches when the caller does not say.
#:
#: Briefly 120,000, which was a mistake made while chasing timeouts: the
#: address it was being tested against had its whole history 300,000 blocks
#: back, so the shallower window returned three address-poisoning dust
#: transfers and none of the case. It said so --- `capped`, with the range ---
#: which is the difference between a wrong answer and a short one, but short
#: was still useless. The span now widens on success instead, so the deeper
#: window costs round trips rather than minutes.
#:
#: Still not genesis. A full-history scan is hundreds of thousands of
#: requests, and the window is named in the `ResultTruncated` message whenever
#: it does not reach the requested start.
_DEFAULT_WINDOW = 400_000

#: Chunks in flight at once. Small on purpose --- see `_scan`.
_SCAN_WORKERS = 4


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
        #: Token contract to (symbol, decimals). Per instance, so a scan asks
        #: each contract once however many transfers it produced.
        self._meta: dict[str, tuple[str, int]] = {}
        #: Block span this endpoint has proved it will serve. Starts cautious,
        #: doubles on success, halves on refusal. Shared across the chunks of a
        #: scan so the limit is discovered once rather than per chunk.
        self._span = _SPAN_START
        #: Widest span this endpoint has been *seen to refuse*, minus room.
        #:
        #: Without it, widening undoes narrowing: succeed at 1,250, double to
        #: 2,500, get refused, halve to 1,250, succeed, double again --- a
        #: wasted failed request every other round, for ever. A refusal is
        #: information about the endpoint's limit and has to be remembered as
        #: such, not just reacted to.
        self._span_cap = _SPAN_CEILING

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """The configured endpoints for this chain, in preference order.

        ``CHAINSCOPE_RPC_<NAME>`` accepts a comma-separated list. One endpoint
        was the original design and it made a single flaky public node a single
        point of failure for a whole chain.

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
            configured = settings.rpc.get(name)
            if not configured:
                continue
            # Comma-separated, and each one becomes its own provider.
            #
            # A single endpoint per chain means one flaky free public node is
            # the difference between the tool working and not working at all.
            # Measured while chasing exactly that: of eleven public BSC
            # endpoints, one served `eth_getLogs` over 5,000 blocks, five
            # capped at a smaller range, two refused the method, two returned
            # 401/403 and one was gone --- and the one that worked timed out
            # under load an hour later, which took the whole capability down
            # because there was nothing behind it.
            #
            # The router already falls over between providers and the circuit
            # breaker already skips an unwell host. Neither could help while
            # there was only ever one. Order is preference order: the first is
            # tried first, and the rest exist for when it is not answering.
            urls = [u.strip() for u in str(configured).split(",") if u.strip()]
            archive = settings.rpc_archive.get(name, False)
            return [
                cls(
                    url,
                    chain,
                    client=client,
                    native_symbol=native_symbol(chain, "ETH"),
                    archive=archive,
                )
                for url in urls
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
        page: int = 1,
    ) -> list[Transfer]:
        """Token movement for an address, reconstructed from ``Transfer`` logs.

        **Tokens only, and the caller has to be told.** A native send emits no
        event and a contract-to-contract call emits no event, so neither is
        here. An address that only ever moved the chain's own coin comes back
        from this empty --- identical, at the call site, to an address that
        never did anything. That is why this declares
        `Capability.TOKEN_TRANSFERS` and not `ASSET_TRANSFERS`: the router keeps
        preferring a real indexer, and a caller that lands here knows the answer
        has a shape.

        This exists because of a case it could not touch. Tracing the LpdFi
        exploit on BSC, every provider refused --- Etherscan's free tier
        excludes the chain and the configured RPC could not enumerate --- so
        `chainscope investigate` reported "could not run" for every analysis
        that needed history. The work got done in ad-hoc scripts calling
        `eth_getLogs`, which is a thing the tool should have been doing.

        **The window is bounded and says so.** Scanning to genesis is hundreds
        of thousands of requests. Absent an explicit ``start_block`` this reads
        back `_DEFAULT_WINDOW` blocks, and if that does not reach what was asked
        for it raises `ResultTruncated` carrying the rows *and naming the range
        it covered* --- because a short answer that does not announce itself is
        the failure this package exists to refuse.

        Ordering is oldest-first, and paging slices that. Logs come back in
        block order per request, and the requests are issued in order, so the
        sequence is stable between runs on the same range.
        """
        head = self._head()
        hi = head if end_block == "latest" else int(end_block)
        want_from = 0 if start_block == "latest" else int(start_block)
        lo = max(want_from, hi - _DEFAULT_WINDOW + 1, 0)

        ways = {"out", "in"} if direction in ("all", "both") else {direction}
        if not ways <= {"out", "in"}:
            raise ProviderError("direction must be 'out', 'in', 'all', or 'both'")

        padded = "0x" + "0" * 24 + address.lower().removeprefix("0x")
        rows: list[Transfer] = []
        seen: set[tuple[str, int]] = set()

        def topics_for(way: str) -> list[Any]:
            # `eth_getLogs` ORs within a topic position and ANDs across them,
            # so "sent by X or received by X" cannot be one request. Two scans
            # are structural; running them one after the other was not.
            topics: list[Any] = [TRANSFER_TOPIC, None, None]
            topics[1 if way == "out" else 2] = padded
            return topics

        # One pool for both directions, not one each.
        #
        # Nesting them multiplied the concurrency: `_scan` already runs
        # `_SCAN_WORKERS` chunks at a time, so a pool per direction put eight
        # requests in flight against a free public endpoint. It timed out, the
        # timeouts counted as host failures, and the circuit breaker opened
        # mid-fetch --- which the page reported as "circuit open" about a node
        # that had been answering a moment earlier.
        #
        # The cap is on the total because that is what the endpoint sees.
        order = sorted(ways)
        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
            # Chunked at the ceiling and let `_chunk` narrow to whatever the
            # endpoint will actually take. Planning at `_SPAN_START` instead
            # would fix the request count at the cautious width and never
            # benefit from an endpoint that turns out to be generous.
            plans = [(way, lo_) for way in order for lo_ in range(lo, hi + 1, _SPAN_CEILING)]
            jobs = [
                pool.submit(
                    self._chunk,
                    chain,
                    lo_,
                    min(lo_ + _SPAN_CEILING - 1, hi),
                    topics_for(way),
                    contract,
                )
                for way, lo_ in plans
            ]
            found = [job.result() for job in jobs]

        for logs in found:
            for log in logs:
                key = (str(log.get("transactionHash", "")), _hexint(log.get("logIndex")))
                if key in seen:
                    continue
                seen.add(key)
                made = self._transfer_from_log(chain, log)
                if made is not None:
                    rows.append(made)

        rows.sort(key=lambda t: (t.block or 0, t.index))
        window = rows[(page - 1) * limit : page * limit]
        if lo > want_from:
            raise ResultTruncated(
                f"{self.name} scanned blocks {lo}..{hi} of the requested "
                f"{want_from}..{hi}. Reading further back means more requests, "
                f"not a different answer --- pass start_block to widen it. "
                f"Native and internal transfers are never included: they emit "
                f"no log.",
                rows=window,
                window_short=True,
            )
        if len(window) == limit and len(rows) > page * limit:
            raise ResultTruncated(
                f"{self.name}: page {page} is full and there is more", rows=window
            )
        return window

    def _head(self) -> int:
        return _hexint(self._call("eth_blockNumber", [], Volatility.LIVE))

    def _chunk(
        self,
        chain: ChainId,
        lo: int,
        hi: int,
        topics: list[Any],
        contract: str | None,
    ) -> list[dict[str, Any]]:
        """One block range, covered in whatever width the endpoint will take.

        **Covers the whole range it was given.** An earlier version fetched a
        single `span`-wide request and returned, silently dropping the rest of
        its assignment --- a gap in the middle of a scan that nothing
        downstream could have detected, since fewer logs is exactly what an
        address with less activity looks like.

        Endpoints cap `eth_getLogs` by range, by result count, or by wall
        clock, and report each differently --- measured against four BSC nodes,
        the workable span ran from 10 blocks to over 20,000 for the same query.
        Halving on any refusal converges on whatever the limit actually is
        without a table of per-host rules to maintain, and the floor stops a
        genuinely broken endpoint from turning one query into thousands.

        The fan-out lives in `asset_transfers`, which holds a single pool for
        both directions: a pool here as well as there multiplied the
        concurrency and tripped the circuit breaker on a healthy node.
        """
        out: list[dict[str, Any]] = []
        at = lo
        while at <= hi:
            # Re-read each time round: another chunk running concurrently may
            # have already learned that this endpoint is more or less generous
            # than we thought.
            span = max(_SPAN_FLOOR, min(self._span, hi - at + 1))
            try:
                out.extend(
                    self.get_logs(
                        chain,
                        address=contract,
                        topics=topics,
                        from_block=at,
                        to_block=min(at + span - 1, hi),
                    )
                )
            except ProviderError:
                if span <= _SPAN_FLOOR:
                    raise
                # Narrower for everyone from here on. Rediscovering the same
                # limit on every chunk is what made narrowing expensive.
                narrower = max(_SPAN_FLOOR, span // 2)
                self._span_cap = min(self._span_cap, narrower)
                self._span = narrower
                continue
            at += span
            # It worked, so try more next time. Doubling rather than jumping
            # to the ceiling, so an endpoint that is merely slow gets to refuse
            # once rather than being asked for eight times what it just served.
            self._span = min(self._span_cap, max(self._span, span * 2))
        return out

    def _token_meta(self, token: str) -> tuple[str, int]:
        """``(symbol, decimals)`` for a token contract, asked once each.

        Two `eth_call`s per **distinct contract**, not per transfer: a scan of
        eighty transfers across four tokens costs eight calls, and they are the
        difference between an analysis and a list of numbers.

        Skipping this was the first version and it cost the thing the scan was
        for. `impersonation` decides whether a token is imitating a real one by
        comparing its *symbol* against the canonical contract for that symbol.
        With no symbol it has nothing to compare, so three counterfeit USDC
        contracts on BSC --- one of them named with invisible characters ---
        came back as "assets the registry says nothing about" instead of as
        lookalikes.

        A contract that does not answer is recorded as answering nothing rather
        than being retried: a token with no `symbol()` is a real thing, and
        `decimals` falls back to 18 only for display.
        """
        cached = self._meta.get(token)
        if cached is not None:
            return cached

        def read_string(sig: str) -> str:
            try:
                raw = self.call(self._chain(), token, sig) or "0x"
            except ProviderError:
                return ""
            body = raw[2:]
            # ABI: offset word, length word, then the bytes. A `bytes32` symbol
            # --- which several older tokens still use --- has no length word,
            # so it is read as the whole word with its padding stripped.
            if len(body) >= 128:
                size = int(body[64:128], 16)
                if size and 128 + size * 2 <= len(body):
                    return bytes.fromhex(body[128 : 128 + size * 2]).decode(
                        "utf-8", errors="replace"
                    )
            if len(body) == 64:
                return bytes.fromhex(body).rstrip(b"\x00").decode("utf-8", errors="replace")
            return ""

        symbol = read_string("0x95d89b41")
        try:
            decimals = _hexint(self.call(self._chain(), token, "0x313ce567"))
        except (ProviderError, ValueError):
            # ValueError as well as ProviderError. A contract with no
            # `decimals()` returns bare `0x`, which `_hexint` cannot parse ---
            # and a ValueError is not a ProviderError, so it escaped the
            # provider layer entirely and took down the whole scan on one
            # unusual token. The Etherscan provider carries a guard for the
            # same shape; this one did not, and a test asking what happens to a
            # silent contract found it.
            decimals = 18
        meta = (symbol, decimals if 0 <= decimals <= 36 else 18)
        self._meta[token] = meta
        return meta

    def _transfer_from_log(self, chain: ChainId, log: dict[str, Any]) -> Transfer | None:
        """One log as a `Transfer`, or None when it is not one.

        An ERC-721 ``Transfer`` carries the token id as a fourth *topic* and an
        empty data field, which decodes as an amount of zero. Returning those
        as fungible movement would put a nonexistent quantity into every total,
        so they are dropped here rather than corrected downstream.

        The token's decimals are not fetched. One `eth_call` per distinct
        contract on a scan that may touch hundreds is a cost the caller has not
        agreed to, and `Amount` carries the raw value either way --- what is
        lost is the display scaling, not the number.
        """
        topics = log.get("topics") or []
        if len(topics) != 3:
            return None
        try:
            raw = int(log.get("data") or "0x0", 16)
        except ValueError:
            return None
        token = str(log.get("address") or "").lower()
        symbol, decimals = self._token_meta(token)
        return Transfer(
            chain=chain,
            tx=TxRef(chain, str(log.get("transactionHash") or "").lower()),
            sender=self._addr("0x" + topics[1][-40:]),
            recipient=self._addr("0x" + topics[2][-40:]),
            amount=Amount(raw, decimals, symbol),
            kind=TransferKind.TOKEN,
            block=_hexint(log.get("blockNumber")),
            index=_hexint(log.get("logIndex")),
            asset=self._addr(token),
        )

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
