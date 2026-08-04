"""Sui provider, over the Foundation's public GraphQL RPC.

Sui answers "what did this address do" directly --- ``transactions`` takes a
``sentAddress`` or ``affectedAddress`` filter --- so unlike EVM there is no gap
between what the RPC offers and what an investigation needs, and no key is
required for address history.

**GraphQL, not JSON-RPC, and not gRPC.** The Foundation disabled JSON-RPC on
its public fullnodes in the week of 27 July 2026; every method this file used
to call now answers "Method not found". Of the two replacements, gRPC means
protobuf over HTTP/2 with a `grpcio` dependency and generated stubs, and it
would bypass this project's entire transport layer --- the response cache, the
audit log, the circuit breaker, the credential scrubber. GraphQL is an HTTP
POST with a JSON body and keeps all of it. Sui positions gRPC for real-time
access and execution and GraphQL for structured queries over live and
historical data, which is what an investigation is.

Only the transport changed. The balance-change pairing below, and the traps in
it, are the same as they were --- which is why the port is a new `_query_blocks`
and not a new provider.

The data it returns is unusually convenient and has one trap in it.

**Balance changes are net, per address, per coin, per transaction.** A
transaction that sends twice to the same recipient produces one change, not two.
That is the right unit for value flow and the wrong one for counting transfers,
so ``transfer_count`` here means transactions touching the pair rather than
individual movements.

**The sender's balance change includes gas.** Sui reports the net effect on the
account, and gas came out of the same balance. Reading that number as the
amount transferred overstates every outbound transfer by the gas cost --- small
per transaction, and systematically wrong across a sweep. The gas is subtracted
back out here using ``effects.gasUsed``, which is why effects are requested even
though only one field is used.

**Nine decimals, not eighteen.** The base unit is MIST. Assuming 18 makes a
balance look a billion times smaller, which is small enough to be mistaken for
dust and skipped.

Pagination is cursor-based rather than offset-based, and the cursor is opaque.
``hasNextPage`` is authoritative; a short page is not evidence of the end.

**Inbound is ``affectedAddress``, which is wider than the old ``ToAddress``.**
It matches any transaction the address took part in, senders included, so the
"in" direction over-collects rather than under-collects. The pairing step
discards anything whose balance change has the wrong sign, so the result is the
same and the cost is requests --- the right way round, since the alternative
misses inbound value that arrived without the address being the named
recipient.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any

from ..chains.base import InvalidAddressError
from ..chains.sui import (
    SUI_DECIMALS,
    SUI_MAINNET,
    SUI_TYPE,
    coin_symbol,
    normalize_address,
    normalize_coin_type,
)
from ..core.chainid import ChainId
from ..core.models import Account, Address, Transaction, Transfer, TransferKind, TxRef
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

__all__ = ["SUI_MAINNET_RPC", "SuiProvider"]

SUI_MAINNET_RPC = "https://fullnode.mainnet.sui.io:443"

#: Public fullnode per network. Keyed by the CAIP-2 reference, which for Sui is
#: the network name rather than a chain number.
#: Foundation fullnodes, kept for a self-hosted or provider node that still
#: serves JSON-RPC --- **not** used as a default. See `from_settings`.
#:
#: The Sui Foundation disabled JSON-RPC on its public fullnodes in the week of
#: 27 July 2026, with the code slated for removal that October. Every method
#: this provider calls now answers:
#:
#:     Method not found. JSON-RPC on public fullnodes has been deprecated.
#:     Please migrate to gRPC or GraphQL endpoints.
#:
#: The protocol is deprecated in the node software rather than removed, so an
#: operator may still serve it and a configured endpoint may still work. What
#: cannot work is the Foundation's public one, and defaulting to it produced a
#: provider that was registered, selected, and certain to fail --- which is the
#: capability-overstatement `Capability` warns about, in the form of a URL.
NETWORK_RPC = {
    "mainnet": "https://graphql.mainnet.sui.io/graphql",
    "testnet": "https://graphql.testnet.sui.io/graphql",
    "devnet": "https://graphql.devnet.sui.io/graphql",
}


#: Endpoints known to have stopped serving JSON-RPC.
#:
#: Kept as a refusal rather than deleted: somebody's config, or a bundle
#: recorded before the switch-off, still names one. Registering a provider
#: against it is worse than registering none --- the router selects it, every
#: call fails, and the failure reads like an outage rather than like a
#: decision somebody made about a protocol.
def _ms(iso: Any) -> int | None:
    """A GraphQL checkpoint timestamp as epoch milliseconds.

    JSON-RPC gave `timestampMs` directly and GraphQL gives ISO-8601, so the
    conversion lives here rather than in the parsing below --- which still
    reads `timestampMs`, because the protocol change is meant to stop at the
    transport.
    """
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


RETIRED = frozenset(
    {
        SUI_MAINNET_RPC,
        "https://fullnode.mainnet.sui.io:443",
        "https://fullnode.testnet.sui.io:443",
        "https://fullnode.devnet.sui.io:443",
    }
)

#: Page size the public fullnodes accept. Asking for more is refused rather
#: than silently reduced.
MAX_PAGE = 50

#: How many times `limit` a *bounded* history query may fetch while paging back
#: to the requested window.
#:
#: Sui's transaction query filters by address and not by checkpoint, so reaching
#: an older window means walking newest-first through everything after it. Ten
#: is a budget rather than an estimate: it is exceeded loudly, and the loud
#: failure is the point --- silently returning the rows that fitted would report
#: an unreachable window as an empty one.
_RANGE_PAGE_BUDGET = 10

#: Hard ceiling on that walk, so one very busy address cannot spend an entire
#: run paging.
_MAX_RANGE_ROWS = 5_000


def is_cacheable(body: Any) -> bool:
    """Whether a JSON-RPC reply is an answer rather than a refusal.

    Same reasoning as the Etherscan provider: an error stored in the cache
    becomes the permanent answer for that address, and re-running does not help
    because re-running makes no request.
    """
    if not isinstance(body, dict):
        return False
    return "error" not in body


class SuiProvider(ReadOnlyProvider):
    """Sui fullnode over JSON-RPC."""

    name = "sui"
    ecosystems = frozenset({"sui"})
    capabilities = (
        Capability.ADDRESS_HISTORY
        | Capability.ASSET_TRANSFERS
        | Capability.TRANSACTION
        | Capability.BALANCE
    )
    cost = CostTier.FREE_PUBLIC

    def __init__(
        self,
        endpoint: str = SUI_MAINNET_RPC,
        *,
        client: Client | None = None,
        chain: ChainId = SUI_MAINNET,
    ) -> None:
        super().__init__(client)
        self.url = endpoint
        self.chain = chain
        self.chains = frozenset({chain})

    @property
    def endpoint(self) -> str:
        return self.url

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """Always available: Sui's public fullnode needs no key.

        The configured endpoint wins if there is one, but the absence of a
        credential is not a reason to return nothing here --- unlike the keyed
        providers, this one works out of the box, and returning ``[]`` would
        leave Sui with no provider at all on a fresh install.

        The endpoint is chosen from the *requested network*. Defaulting to
        mainnet meant a ``sui:testnet`` query silently read mainnet and then
        tagged and cached the answers as testnet --- data from the wrong network
        under the right label, which survives in the store long after the
        session that produced it. An unrecognised network gets no default at
        all, because guessing one is how that happens again.
        """
        if not cls.serves(chain):
            return []
        # A default again, now that there is a public endpoint that answers.
        #
        # It was removed an hour ago and correctly: the JSON-RPC default had
        # been switched off, so registering against it promised a capability
        # the provider could not deliver. The Foundation's GraphQL RPC is
        # public, needs no key, and answers --- so the default is a promise
        # that holds, and requiring configuration for a working public endpoint
        # would be its own kind of wrong.
        url = settings.rpc.get("sui") or NETWORK_RPC.get(chain.reference)
        if not url:
            return []
        if url.rstrip("/") in RETIRED:
            raise ProviderError(
                f"{url} no longer serves JSON-RPC --- the Sui Foundation "
                f"disabled it on its public fullnodes in July 2026. Point "
                f"CHAINSCOPE_RPC_SUI at a provider or self-hosted node that "
                f"still serves it, or wait for the GraphQL provider. Leaving "
                f"this configured would register a source that fails every "
                f"call and looks like an outage"
            )
        return [cls(url, client=client, chain=chain)]

    # ---------------------------------------------------------------- request

    _HISTORY = """
query History($a: SuiAddress!, $n: Int!, $after: String) {
  transactions(filter: {FILTER: $a}, first: $n, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      digest
      effects {
        status
        checkpoint { sequenceNumber timestamp }
        gasEffects { gasSummary { computationCost storageCost storageRebate } }
        balanceChanges { nodes { owner { address } coinType { repr } amount } }
      }
    }
  }
}"""

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        volatility: Volatility = Volatility.SETTLED,
    ) -> Any:
        """One GraphQL request, through the same transport as everything else.

        `Client.rpc` is a JSON POST with a cache key, an audit entry, a circuit
        breaker and a credential scrubber attached. GraphQL is a JSON POST.
        Reusing it is the whole reason this is GraphQL and not gRPC.

        **A GraphQL 200 can carry errors.** Unlike JSON-RPC there is no
        transport-level failure to trip on: the body arrives with `data: null`
        and an `errors` array, and a caller reading `data` sees an absence. So
        errors are raised here, before anything downstream can read the shape
        as an empty history.
        """
        try:
            body = self.client.post_json(
                self.url,
                {"query": query, "variables": variables},
                volatility=volatility,
                provider=self.name,
                # Scope by chain, not URL: two endpoints serving mainnet must
                # share cache entries, and a bundle recorded against one has to
                # replay against the other.
                scope=str(self.chain),
            )
        except TransportError as exc:
            raise ProviderError(f"graphql: {exc}") from exc

        if not isinstance(body, dict):
            raise ProviderError(f"graphql: unexpected response shape {type(body).__name__}")
        if body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) else body["errors"]
            message = (first or {}).get("message") if isinstance(first, dict) else str(first)
            raise ProviderError(f"graphql: {message}")
        data = body.get("data")
        if data is None:
            raise ProviderError("graphql: the response carried no data and no error")
        if not isinstance(data, dict):
            # A `ProviderError`, not whatever `.get` raises on a string. An
            # AttributeError escapes the provider layer, so `Router.dispatch`
            # cannot fall back and one malformed body takes down the call ---
            # the same shape as the `_hexint` crash on a token with no
            # `decimals()`, which is why the guard is here and not at the call
            # site.
            raise ProviderError(
                f"graphql: unexpected response shape "
                f"--- data is {type(data).__name__}, not an object"
            )
        return data

    def _query_blocks(
        self, address: str, *, direction: str, limit: int
    ) -> list[dict[str, Any]]:
        """Paginate ``transactions`` for one address, oldest cursor onward.

        ``hasNextPage`` decides when to stop. A page shorter than requested is
        not evidence of the end --- the service may return fewer for its own
        reasons --- and treating it as such is how a history silently ends
        early.

        **The first page is not settled data.** It contains the tip, and an
        address's history is a changing aggregate: cached for thirty days a
        second look a week later returns the same answer and silently misses
        everything since. Later pages are reached through a cursor into older
        checkpoints, which do not change, so those keep the long TTL --- the
        deep history of a busy address is exactly what is worth caching hard.

        Rows are reshaped into what the pairing step already expects, so the
        protocol change stops here.
        """
        field = "sentAddress" if direction == "out" else "affectedAddress"
        query = self._HISTORY.replace("FILTER", field)
        owner = normalize_address(address)
        out: list[dict[str, Any]] = []
        cursor: str | None = None

        while len(out) < limit:
            data = self._graphql(
                query,
                {"a": owner, "n": min(MAX_PAGE, limit - len(out)), "after": cursor},
                volatility=Volatility.SLOW if cursor is None else Volatility.SETTLED,
            )
            page = (data or {}).get("transactions") or {}
            for node in page.get("nodes") or []:
                out.append(self._as_row(node))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
            if not cursor:
                # hasNextPage true with no cursor would loop forever on the
                # same page.
                break

        return out[:limit]

    @staticmethod
    def _as_row(node: dict[str, Any]) -> dict[str, Any]:
        """One GraphQL transaction as the dict the pairing step reads.

        Deliberately a translation and not a redesign. The balance-change
        pairing below is the part with the hard-won corrections in it --- net
        changes, gas inside the sender's delta, nine decimals --- and rewriting
        it alongside the transport would have made a protocol migration into a
        rewrite of the logic that migration exists to preserve.
        """
        effects = node.get("effects") or {}
        summary = ((effects.get("gasEffects") or {}).get("gasSummary")) or {}
        checkpoint = effects.get("checkpoint") or {}
        changes = []
        for change in (effects.get("balanceChanges") or {}).get("nodes") or []:
            owner = (change.get("owner") or {}).get("address")
            coin = (change.get("coinType") or {}).get("repr")
            changes.append(
                {
                    # Rewrapped into the tagged-union shape `_owner` reads. The
                    # GraphQL `owner` is already an account or absent, so an
                    # object owner arrives as None and is dropped there rather
                    # than entering the graph as though somebody held it.
                    "owner": {"AddressOwner": owner} if owner else None,
                    "coinType": coin or SUI_TYPE,
                    "amount": change.get("amount"),
                }
            )
        sender = (node.get("sender") or {}).get("address")
        return {
            "digest": node.get("digest"),
            # Rewrapped into the shapes the readers below already expect ---
            # `_sender_of` looks under `transaction.data`, and the success test
            # reads `effects.status.status` in lower case. GraphQL gives a flat
            # `sender` and an upper-case `status`, and translating both here is
            # what keeps the protocol change inside the transport. The first
            # draft left them in GraphQL's shape, which made every transaction
            # senderless and unsuccessful --- both silently, since "no sender"
            # and "failed" are values those fields legitimately take.
            "transaction": {"data": {"sender": sender}} if sender else {},
            # The checkpoint number is what `block` is built from. Dropping it
            # in the first draft of this translation cost every Sui transfer
            # its position: ordering, `_known_range`, and the block column in
            # every view all read None, which looks like data that simply has
            # no block rather than a field the translation forgot.
            "checkpoint": checkpoint.get("sequenceNumber"),
            "timestampMs": _ms(checkpoint.get("timestamp")),
            "balanceChanges": changes,
            "effects": {
                "gasUsed": summary,
                "status": {"status": str(effects.get("status") or "").lower()},
            },
        }

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _owner(entry: Any) -> str | None:
        """Owner address from a balance change.

        Sui's owner field is a tagged union: ``{"AddressOwner": "0x…"}`` for an
        account, ``{"ObjectOwner": …}`` or ``{"Shared": …}`` for objects. Only
        the first is an address, and treating an object id as one puts a
        non-account into the graph as though somebody controlled it.
        """
        if isinstance(entry, dict):
            owner = entry.get("AddressOwner")
            if isinstance(owner, str):
                return normalize_address(owner)
        return None

    @staticmethod
    def _gas_cost(tx: dict[str, Any]) -> int:
        """Total MIST the sender paid, including the storage rebate.

        The rebate is returned to the sender, so the net cost is
        computation + storage - rebate. Ignoring the rebate overstates gas,
        which then under-reports the transfer it is subtracted from.
        """
        used = (tx.get("effects") or {}).get("gasUsed") or {}
        try:
            return (
                int(used.get("computationCost", 0))
                + int(used.get("storageCost", 0))
                - int(used.get("storageRebate", 0))
            )
        except (TypeError, ValueError):
            return 0

    def _asset(self, coin_type: str) -> Address:
        """Asset identity for a coin type.

        The full ``package::module::name``, because a package can define more
        than one coin and the package alone is not an identity. Stored in
        ``raw`` with the package as the comparison key's prefix so it still
        sorts and groups sensibly.
        """
        return Address(self.chain, coin_type, coin_type.lower())

    def _address(self, raw: str | None) -> Address | None:
        if not raw:
            return None
        return Address(self.chain, raw, normalize_address(raw))

    @staticmethod
    def _when(tx: dict[str, Any]) -> datetime | None:
        ms = tx.get("timestampMs")
        if ms in (None, ""):
            return None
        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- queries

    def asset_transfers(
        self,
        chain: ChainId,
        address: str,
        *,
        direction: str = "out",
        limit: int = 100,
        **_: Any,
    ) -> list[Transfer]:
        """Value movements derived from per-transaction balance changes.

        Each transaction yields at most one transfer per (counterparty, coin),
        because that is the granularity Sui reports. Pairing is by sign: the
        subject's change is negative for an outbound transfer and the
        counterparty's is positive.
        """
        if direction not in ("out", "in", "all"):
            raise ProviderError(f"direction must be 'out', 'in', or 'all', not {direction!r}")

        owner = normalize_address(address)
        wanted = ("out", "in") if direction == "all" else (direction,)
        seen: set[tuple[str, str, str]] = set()
        per_tx: dict[str, int] = {}
        transfers: list[Transfer] = []

        for way in wanted:
            for tx in self._query_blocks(address, direction=way, limit=limit):
                digest = str(tx.get("digest") or "")
                changes = tx.get("balanceChanges") or []
                gas = self._gas_cost(tx)

                mine: dict[str, int] = {}
                others: dict[str, list[tuple[str, int]]] = {}
                for change in changes:
                    who = self._owner(change.get("owner"))
                    if who is None:
                        continue
                    try:
                        amount = int(change.get("amount", 0))
                    except (TypeError, ValueError):
                        continue
                    coin = str(change.get("coinType") or SUI_TYPE)
                    # A coin type this parser cannot canonicalise is still an
                    # identity. Keeping the raw string groups it consistently;
                    # dropping the change would lose value movement entirely,
                    # which is the worse of the two failures.
                    with contextlib.suppress(InvalidAddressError):
                        coin = normalize_coin_type(coin)
                    if who == owner:
                        mine[coin] = mine.get(coin, 0) + amount
                    else:
                        others.setdefault(coin, []).append((who, amount))

                for coin, delta in mine.items():
                    # Gas came out of the same balance and is not a transfer.
                    native = coin == normalize_coin_type(SUI_TYPE)
                    if native and delta < 0:
                        delta += gas
                    if delta == 0:
                        continue
                    outgoing = delta < 0
                    for who, other_delta in others.get(coin, []):
                        # Opposite signs, or the pair is not a counterparty.
                        if (other_delta > 0) is not outgoing:
                            continue
                        key = (digest, who, coin)
                        if key in seen:
                            continue
                        seen.add(key)
                        # A stable position within the transaction. Sui has no
                        # log index, so leaving this at zero made every transfer
                        # in a block share one -- and the store's identity key
                        # then could not tell two of them apart on amount alone.
                        # Counted per digest so the value does not shift when a
                        # different page of history is fetched.
                        per_tx[digest] = per_tx.get(digest, -1) + 1
                        transfers.append(
                            Transfer(
                                chain=self.chain,
                                tx=TxRef(self.chain, digest),
                                sender=self._address(owner if outgoing else who),
                                recipient=self._address(who if outgoing else owner),
                                # Decimals are known only for SUI. Giving a
                                # token the native nine renders a six-decimal
                                # balance a thousand times too small --- the
                                # exact silent error this module's docstring
                                # warns about, committed by this module. An
                                # unknown coin is reported in base units, which
                                # is awkward and correct.
                                amount=Amount(
                                    abs(other_delta),
                                    SUI_DECIMALS if native else 0,
                                    coin_symbol(coin),
                                ),
                                kind=TransferKind.NATIVE if native else TransferKind.TOKEN,
                                index=per_tx[digest],
                                timestamp=self._when(tx),
                                block=int(tx["checkpoint"]) if tx.get("checkpoint") else None,
                                # The whole coin type, not just its package. One
                                # package can define several coins, and keying
                                # on the package alone collapses them into a
                                # single asset in the store and the graph.
                                asset=None if native else self._asset(coin),
                            )
                        )

        transfers.sort(key=lambda t: (t.block or 0, t.index))
        return transfers

    def address_history(
        self,
        chain: ChainId,
        address: str,
        *,
        start_block: int = 0,
        end_block: int | str = "latest",
        limit: int = 100,
    ) -> list[Transaction]:
        """Transactions touching an address, each carrying its transfers.

        ``recipient`` is always ``None`` here, and that is not a gap being
        papered over: a Sui transaction has no single counterparty. It is a
        programmable block that can touch many addresses and coins, so the
        detail lives in :attr:`Transaction.transfers` and the top-level
        ``value`` is the net native movement for the subject address, corrected
        for gas.

        ``start_block``/``end_block`` are filtered client-side against the
        checkpoint number, because Sui's transaction query filters by address
        and not by range.

        **That is not free, and it used to be treated as though it were.** The
        query returns newest-first, so fetching `limit` rows and *then* applying
        the range means an address with more than `limit` transactions newer
        than the window returns **nothing** for it --- and nothing reads as "no
        activity in that period", which is the failure this package exists to
        prevent. A window nobody could reach is not a window that was empty.

        So a bounded window paginates until it has passed the range rather than
        until it has `limit` rows, and raises :class:`ResultTruncated` if the
        page budget runs out first. Refusing is the only honest answer there:
        the alternative is a short list that looks complete.
        """
        owner = normalize_address(address)
        low = start_block
        high = None if end_block == "latest" else int(end_block)
        bounded = low > 0 or high is not None
        # A bounded window needs enough rows to *reach* the range, not `limit`
        # rows from the tip. The multiplier is a budget, not a guess at the
        # answer --- exceeding it raises rather than truncating quietly.
        fetch = min(limit * _RANGE_PAGE_BUDGET, _MAX_RANGE_ROWS) if bounded else limit
        transfers = self.asset_transfers(chain, address, direction="all", limit=fetch)

        grouped: dict[str, list[Transfer]] = {}
        for t in transfers:
            grouped.setdefault(t.tx.hash, []).append(t)

        out: list[Transaction] = []
        for digest, group in grouped.items():
            block = next((t.block for t in group if t.block is not None), None)
            if block is not None and (block < low or (high is not None and block > high)):
                continue
            native_out = sum(
                t.amount.raw
                for t in group
                if t.kind is TransferKind.NATIVE and t.sender and t.sender.key == owner
            )
            out.append(
                Transaction(
                    ref=TxRef(self.chain, digest),
                    sender=self._address(owner),
                    recipient=None,
                    value=Amount(native_out, SUI_DECIMALS, "SUI"),
                    timestamp=next((t.timestamp for t in group if t.timestamp), None),
                    block=block,
                    transfers=tuple(group),
                )
            )
        out.sort(key=lambda t: t.block or 0)

        if bounded and len(transfers) >= fetch:
            # The budget ran out before the walk passed the window, so anything
            # older than the oldest row fetched was never looked at. Returning
            # `out` here would be a list that looks like the answer.
            reached = min((t.block for t in transfers if t.block is not None), default=None)
            if reached is None or reached > low:
                raise ResultTruncated(
                    f"paged back to checkpoint {reached} of the requested "
                    f"{low}-{high if high is not None else 'latest'} after "
                    f"{fetch} rows, and stopped at the budget. Sui filters by "
                    f"address, not by range, so an older window costs a walk "
                    f"through everything newer --- narrow the window, or raise "
                    f"the limit."
                )
        return out

    def get_transaction(self, chain: ChainId, tx_hash: str) -> Transaction:
        """One transaction block by digest.

        The class declared ``Capability.TRANSACTION`` while inheriting the base
        implementation, which refuses. The router read the declaration, picked
        this provider, and got "sui does not provide transactions" --- and since
        it is the only Sui provider, there was nothing to fall back to. An
        analyzer checking ``applicable()`` was told yes and then failed at the
        point of use, which is the expensive place to find out.

        ``sender`` is the transaction's actual sender here, unlike
        :meth:`address_history` where it is the subject being queried. There is
        still no single ``recipient``: a programmable transaction block can
        touch many addresses, so the detail is in ``transfers``.
        """
        data = self._graphql(
            """
query Tx($d: String!) {
  transaction(digest: $d) {
    digest
    sender { address }
    effects {
      status
      checkpoint { timestamp }
      gasEffects { gasSummary { computationCost storageCost storageRebate } }
      balanceChanges { nodes { owner { address } coinType { repr } amount } }
    }
  }
}""",
            {"d": tx_hash},
        )
        node = (data or {}).get("transaction")
        if not isinstance(node, dict) or not node.get("digest"):
            raise ProviderError(f"transaction {tx_hash} not found")
        raw = self._as_row(node)
        sender = self._sender_of(raw)
        gas = self._gas_cost(raw)
        transfers = self._transfers_from(raw, subject=sender)

        # The sender's own net native movement, gas removed. Without that
        # correction every transaction looks like it moved its gas cost.
        native_out = -sum(
            int(c.get("amount", 0))
            for c in (raw.get("balanceChanges") or [])
            if self._owner(c.get("owner")) == sender
            and normalize_coin_type(str(c.get("coinType") or SUI_TYPE))
            == normalize_coin_type(SUI_TYPE)
        )
        status = ((raw.get("effects") or {}).get("status") or {}).get("status")
        return Transaction(
            ref=TxRef(self.chain, str(raw["digest"])),
            sender=self._address(sender),
            recipient=None,
            value=Amount(max(0, native_out - gas), SUI_DECIMALS, "SUI"),
            timestamp=self._when(raw),
            block=int(raw["checkpoint"]) if raw.get("checkpoint") else None,
            # Absent status is not success. Sui reports it in effects, and
            # defaulting to True would silently turn every unparsed reply into a
            # transaction that worked.
            success=status == "success",
            fee=Amount(max(0, gas), SUI_DECIMALS, "SUI"),
            transfers=transfers,
        )

    @staticmethod
    def _sender_of(tx: dict[str, Any]) -> str:
        data = (tx.get("transaction") or {}).get("data") or {}
        sender = data.get("sender")
        return normalize_address(str(sender)) if sender else ""

    def _transfers_from(self, tx: dict[str, Any], *, subject: str) -> tuple[Transfer, ...]:
        """Pair balance changes into transfers for one transaction block.

        Same sign-pairing rule as :meth:`asset_transfers`, applied to a single
        already-fetched block rather than a query result.
        """
        digest = str(tx.get("digest") or "")
        gas = self._gas_cost(tx)
        native_type = normalize_coin_type(SUI_TYPE)

        mine: dict[str, int] = {}
        others: dict[str, list[tuple[str, int]]] = {}
        for change in tx.get("balanceChanges") or []:
            who = self._owner(change.get("owner"))
            if who is None:
                continue
            try:
                amount = int(change.get("amount", 0))
            except (TypeError, ValueError):
                continue
            coin = str(change.get("coinType") or SUI_TYPE)
            with contextlib.suppress(InvalidAddressError):
                coin = normalize_coin_type(coin)
            if who == subject:
                mine[coin] = mine.get(coin, 0) + amount
            else:
                others.setdefault(coin, []).append((who, amount))

        out: list[Transfer] = []
        seen: set[tuple[str, str]] = set()
        position = 0
        for coin, delta in mine.items():
            native = coin == native_type
            if native and delta < 0:
                delta += gas
            if delta == 0:
                continue
            outgoing = delta < 0
            for who, other_delta in others.get(coin, []):
                if (other_delta > 0) is not outgoing:
                    continue
                if (who, coin) in seen:
                    continue
                seen.add((who, coin))
                # A stable position, matching `asset_transfers`. Sui has no log
                # index, and leaving every transfer at zero meant the two
                # methods in this provider disagreed about what `index` means
                # --- a trap for anything comparing their output.
                #
                # Not currently a loss: the store's identity key includes
                # `asset`, so two coin types cannot collide. That is the store
                # covering for the provider, and a provider should not need it.
                position += 1
                out.append(
                    Transfer(
                        chain=self.chain,
                        tx=TxRef(self.chain, digest),
                        sender=self._address(subject if outgoing else who),
                        recipient=self._address(who if outgoing else subject),
                        amount=Amount(
                            abs(other_delta),
                            SUI_DECIMALS if native else 0,
                            coin_symbol(coin),
                        ),
                        kind=TransferKind.NATIVE if native else TransferKind.TOKEN,
                        index=position - 1,
                        timestamp=self._when(tx),
                        block=int(tx["checkpoint"]) if tx.get("checkpoint") else None,
                        asset=None if native else self._asset(coin),
                    )
                )
        return tuple(out)

    def get_account(self, chain: ChainId, address: str) -> Account:
        """Balances for an address.

        Only the native balance goes in :attr:`Account.balance`; other coins
        would need a symbol and decimals lookup per type, and guessing either is
        an order-of-magnitude error waiting to happen.
        """
        owner = normalize_address(address)
        data = self._graphql(
            """
query Bal($a: SuiAddress!, $c: String!) {
  address(address: $a) { balance(coinType: $c) { totalBalance } }
}""",
            {"a": owner, "c": SUI_TYPE},
            volatility=Volatility.LIVE,
        )
        total = 0
        held = ((data or {}).get("address") or {}).get("balance") or {}
        try:
            total = int(held.get("totalBalance") or 0)
        except (TypeError, ValueError):
            total = 0
        return Account(
            address=Address(self.chain, address, owner),
            balance=Amount(total, SUI_DECIMALS, "SUI"),
            # Sui has no nonce, so completeness cannot be checked this way. None
            # is the honest answer; zero would read as "no transactions".
            tx_count=None,
            is_contract=False,
        )

    def balances(self, address: str) -> list[Amount]:
        """Every coin the address holds.

        Decimals are only known for SUI, so other coins are reported in their
        base unit with the coin's symbol. That is deliberately awkward: the
        alternative is to assume nine and be wrong by orders of magnitude for
        most tokens.
        """
        owner = normalize_address(address)
        data = self._graphql(
            """
query All($a: SuiAddress!) {
  address(address: $a) {
    balances(first: 50) { nodes { coinType { repr } totalBalance } }
  }
}""",
            {"a": owner},
            volatility=Volatility.LIVE,
        )
        nodes = (((data or {}).get("address") or {}).get("balances") or {}).get("nodes")
        if not isinstance(nodes, list):
            return []
        out: list[Amount] = []
        for entry in nodes:
            if not isinstance(entry, dict):
                continue
            coin = str((entry.get("coinType") or {}).get("repr") or "")
            try:
                total = int(entry.get("totalBalance", 0))
            except (TypeError, ValueError):
                continue
            native = coin and normalize_coin_type(coin) == normalize_coin_type(SUI_TYPE)
            out.append(Amount(total, SUI_DECIMALS if native else 0, coin_symbol(coin)))
        return out
