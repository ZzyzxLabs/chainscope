"""Sui provider.

Sui answers "what did this address do" directly --- ``suix_queryTransactionBlocks``
takes a ``FromAddress`` or ``ToAddress`` filter --- so unlike EVM there is no gap
between what JSON-RPC offers and what an investigation needs, and no explorer
key is required for address history.

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
from .base import Capability, CostTier, ProviderError, ReadOnlyProvider

__all__ = ["SUI_MAINNET_RPC", "SuiProvider"]

SUI_MAINNET_RPC = "https://fullnode.mainnet.sui.io:443"

#: Page size the public fullnodes accept. Asking for more is refused rather
#: than silently reduced.
MAX_PAGE = 50


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

    # ---------------------------------------------------------------- request

    def _rpc(
        self,
        method: str,
        params: list[Any],
        *,
        volatility: Volatility = Volatility.SETTLED,
    ) -> Any:
        try:
            return self.client.rpc(
                self.url,
                method,
                params,
                volatility=volatility,
                provider=self.name,
                # Scope by chain, not URL: two fullnodes serving mainnet must
                # share cache entries, and a bundle recorded against one has to
                # replay against the other.
                scope=str(self.chain),
            )
        except TransportError as exc:
            raise ProviderError(f"{method}: {exc}") from exc

    def _query_blocks(
        self, address: str, *, direction: str, limit: int
    ) -> list[dict[str, Any]]:
        """Paginate ``suix_queryTransactionBlocks`` for one address.

        ``hasNextPage`` decides when to stop. A page shorter than requested is
        not evidence of the end --- the node may return fewer for its own
        reasons --- and treating it as such is how a history silently ends
        early.
        """
        key = "FromAddress" if direction == "out" else "ToAddress"
        owner = normalize_address(address)
        out: list[dict[str, Any]] = []
        cursor: Any = None

        while len(out) < limit:
            page = self._rpc(
                "suix_queryTransactionBlocks",
                [
                    {
                        "filter": {key: owner},
                        "options": {
                            "showBalanceChanges": True,
                            # Only gasUsed is read, but without effects the
                            # sender's change cannot be corrected for gas.
                            "showEffects": True,
                            "showInput": False,
                        },
                    },
                    cursor,
                    min(MAX_PAGE, limit - len(out)),
                    True,  # descending: most recent first
                ],
            )
            if not isinstance(page, dict):
                raise ProviderError("queryTransactionBlocks: unexpected response shape")

            out.extend(page.get("data") or [])
            if not page.get("hasNextPage"):
                break
            cursor = page.get("nextCursor")
            if cursor is None:
                # hasNextPage true with no cursor would loop forever on the
                # same page.
                break

        return out[:limit]

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

        ``start_block``/``end_block`` are accepted for interface compatibility
        and filtered client-side against the checkpoint number. Sui's
        transaction query filters by address, not by range, so narrowing here
        saves no requests --- it only avoids returning rows the caller did not
        ask for.
        """
        owner = normalize_address(address)
        transfers = self.asset_transfers(chain, address, direction="all", limit=limit)

        grouped: dict[str, list[Transfer]] = {}
        for t in transfers:
            grouped.setdefault(t.tx.hash, []).append(t)

        low = start_block
        high = None if end_block == "latest" else int(end_block)

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
        return out

    def get_account(self, chain: ChainId, address: str) -> Account:
        """Balances for an address.

        Only the native balance goes in :attr:`Account.balance`; other coins
        would need a symbol and decimals lookup per type, and guessing either is
        an order-of-magnitude error waiting to happen.
        """
        owner = normalize_address(address)
        body = self._rpc(
            "suix_getBalance",
            [owner, SUI_TYPE],
            volatility=Volatility.LIVE,
        )
        total = 0
        if isinstance(body, dict):
            try:
                total = int(body.get("totalBalance", 0))
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
        body = self._rpc("suix_getAllBalances", [owner], volatility=Volatility.LIVE)
        if not isinstance(body, list):
            return []
        out: list[Amount] = []
        for entry in body:
            if not isinstance(entry, dict):
                continue
            coin = str(entry.get("coinType") or "")
            try:
                total = int(entry.get("totalBalance", 0))
            except (TypeError, ValueError):
                continue
            native = coin and normalize_coin_type(coin) == normalize_coin_type(SUI_TYPE)
            out.append(Amount(total, SUI_DECIMALS if native else 0, coin_symbol(coin)))
        return out
