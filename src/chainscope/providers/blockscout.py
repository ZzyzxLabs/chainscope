"""Blockscout: a second opinion that needs no API key.

Every other explorer-class provider here wants a credential, which means a
one-provider deployment is the normal one --- and a single source cannot be
checked against anything. This exists to make corroboration possible at all;
see :meth:`chainscope.providers.router.Router.corroborate`.

**Why this source specifically.** Field notes from a real multi-chain trace
recorded that a popular archive endpoint's ``eth_getLogs`` *silently drops
records*: the same query returns a log when asked for one block and loses it
when asked in five-hundred-block ranges, with no error and no indication that
anything is missing. One withdrawal address out of thirteen went unnoticed that
way. The same notes measured Blockscout's ``module=logs`` as complete over the
identical range, and it is the reason the rule became "enumerate from two
independent sources, always".

Blockscout speaks a dialect close enough to Etherscan's V1 API that the parsing
is familiar, and different enough to be worth stating:

* ``status: "0"`` with ``"No transactions found"`` means no data, exactly as on
  Etherscan --- and, exactly as on Etherscan, must not be cached as an error or
  read as one.
* There is no ``chainid`` parameter. Each chain is a separate host, so the
  instance is bound to one.
* Name tags are not exposed. ``name`` and ``public_tags`` come back ``null``
  for addresses that are clearly labelled in the web UI, so this provider is a
  source of *movements*, never of attribution. Claiming otherwise would put an
  empty label where a real one exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.chainid import ARBITRUM, BASE, ETHEREUM, GNOSIS, OPTIMISM, POLYGON, ChainId
from ..core.models import Address, Transaction, Transfer, TransferKind, TxRef
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

__all__ = ["BLOCKSCOUT_HOSTS", "BlockscoutProvider", "is_cacheable"]

#: Public instance per chain. Each is a separate deployment --- there is no
#: multi-chain parameter --- so a chain absent here has no Blockscout to reach.
#:
#: BSC is deliberately missing: there is no Blockscout instance for it. Field
#: notes record `bsc.blockscout.com` returning 404, which is worth writing down
#: because guessing the hostname pattern is the obvious thing to try.
BLOCKSCOUT_HOSTS: dict[ChainId, str] = {
    ETHEREUM: "https://eth.blockscout.com",
    OPTIMISM: "https://optimism.blockscout.com",
    POLYGON: "https://polygon.blockscout.com",
    BASE: "https://base.blockscout.com",
    ARBITRUM: "https://arbitrum.blockscout.com",
    GNOSIS: "https://gnosis.blockscout.com",
}

_NO_DATA = ("no transactions found", "no records found", "no logs found")


def is_cacheable(body: Any) -> bool:
    """Whether a response is an answer rather than a refusal.

    Same reasoning as the Etherscan provider: a rate limit arrives as ``200 OK``
    with ``status: "0"``, and storing that makes it the cached answer for the
    address. "No transactions found" shares the status and *is* an answer, so
    the two are told apart by message rather than by status.
    """
    if not isinstance(body, dict):
        return False
    if body.get("status") == "1":
        return True
    return any(n in str(body.get("message", "")).lower() for n in _NO_DATA)


class BlockscoutProvider(ReadOnlyProvider):
    """Explorer-backed history from a public Blockscout instance."""

    name = "blockscout"
    ecosystems = frozenset({"eip155"})
    capabilities = (
        Capability.ADDRESS_HISTORY
        | Capability.ASSET_TRANSFERS
        | Capability.TRANSACTION
        | Capability.LOGS
    )
    #: Free and unkeyed, so the router reaches for it before anything metered.
    cost = CostTier.FREE_PUBLIC

    def __init__(
        self,
        chain: ChainId = ETHEREUM,
        *,
        base_url: str = "",
        client: Client | None = None,
        native_symbol: str = "ETH",
    ) -> None:
        super().__init__(client)
        url = base_url or BLOCKSCOUT_HOSTS.get(chain, "")
        if not url:
            raise ValueError(
                f"no public Blockscout instance for {chain}. Pass base_url to "
                f"point at a private one."
            )
        self.base_url = url.rstrip("/")
        self.chains = frozenset({chain})
        self.native_symbol = native_symbol

    @property
    def endpoint(self) -> str:
        return self.base_url

    @classmethod
    def serves(cls, chain: ChainId) -> bool:
        # Narrower than the ecosystem: an EVM chain with no public instance
        # cannot be served, and saying otherwise makes the router pick this and
        # fail instead of choosing something that works.
        return chain in BLOCKSCOUT_HOSTS

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """Always available where an instance exists: no credential to check.

        That is the point of including it. Corroboration needs a second source
        that is present by default, because a mechanism that only works for
        users who configured two API keys is a mechanism that does not run.
        """
        from ..core.chainid import native_symbol

        if not cls.serves(chain):
            return []
        return [cls(chain, client=client, native_symbol=native_symbol(chain, "ETH"))]

    # ---------------------------------------------------------------- request

    def _get(self, module: str, action: str, **params: Any) -> Any:
        try:
            body = self.client.get(
                f"{self.base_url}/api",
                {"module": module, "action": action, **params},
                volatility=Volatility.SLOW,
                provider=self.name,
                cacheable=is_cacheable,
            )
        except TransportError as exc:
            raise ProviderError(str(exc)) from exc

        if not isinstance(body, dict):
            raise ProviderError(f"unexpected response shape: {type(body).__name__}")

        if body.get("status") == "0":
            message = str(body.get("message", "")).lower()
            if any(n in message for n in _NO_DATA):
                return []
            # Anything else with status 0 is a refusal, and returning an empty
            # list here would be indistinguishable from "this address did
            # nothing" --- the failure this whole package is arranged around.
            raise ProviderError(
                f"blockscout refused: {body.get('message') or body.get('result')}"
            )

        result = body.get("result")
        return result if result is not None else []

    # ---------------------------------------------------------------- helpers

    def _chain(self) -> ChainId:
        return next(iter(self.chains))

    def _addr(self, raw: str | None) -> Address | None:
        if not raw:
            return None
        return Address(self._chain(), raw, raw.lower())

    @staticmethod
    def _when(row: dict[str, Any]) -> datetime | None:
        stamp = row.get("timeStamp") or row.get("timestamp")
        if not stamp:
            return None
        try:
            return datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
        """Ordinary transactions an address sent or received."""
        rows = self._get(
            "account",
            "txlist",
            address=address,
            startblock=start_block,
            endblock=999_999_999 if end_block == "latest" else int(end_block),
            sort="asc",
        )
        if not isinstance(rows, list):
            raise ProviderError("txlist did not return a list")

        if len(rows) >= limit:
            # Refused, not sliced. A source that quietly returns `limit` rows
            # and stops is the exact failure Router.corroborate exists to
            # detect -- and a corroboration source that truncates silently
            # poisons the mechanism built to catch truncation. Etherscan
            # already refuses here; the two must agree on what a full page
            # means or corroborating them compares a capped list to an
            # uncapped one and calls the difference a disagreement.
            raise ResultTruncated(
                f"txlist returned {len(rows)} records, at or above the {limit} "
                f"requested. There is very likely more. Narrow the block range "
                f"and query again; any total from this set is a lower bound."
            )

        out: list[Transaction] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("hash"):
                continue
            out.append(
                Transaction(
                    ref=TxRef(chain, str(row["hash"]).lower()),
                    sender=self._addr(row.get("from")),
                    recipient=self._addr(row.get("to")),
                    value=Amount(self._int(row.get("value")) or 0, 18, self.native_symbol),
                    timestamp=self._when(row),
                    block=self._int(row.get("blockNumber")),
                    # isError is "1" on revert, as on Etherscan. A reverted
                    # transaction moved nothing, and counting it inflates every
                    # total derived from this list.
                    success=str(row.get("isError", "0")) != "1",
                    nonce=self._int(row.get("nonce")),
                    input_data=str(row.get("input") or ""),
                )
            )
        return out

    def asset_transfers(
        self,
        chain: ChainId,
        address: str,
        *,
        direction: str = "out",
        limit: int = 1000,
        **_: Any,
    ) -> list[Transfer]:
        """ERC-20 movements touching an address.

        ``direction`` filters after fetching, because the endpoint has no side
        parameter --- it returns both and the caller narrows.
        """
        if direction not in ("out", "in", "all"):
            raise ProviderError(f"direction must be 'out', 'in', or 'all', not {direction!r}")

        rows = self._get("account", "tokentx", address=address, sort="asc")
        if not isinstance(rows, list):
            raise ProviderError("tokentx did not return a list")

        key = address.lower()
        out: list[Transfer] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("hash"):
                continue
            sender, recipient = row.get("from", ""), row.get("to", "")
            if direction == "out" and str(sender).lower() != key:
                continue
            if direction == "in" and str(recipient).lower() != key:
                continue
            decimals = self._int(row.get("tokenDecimal"))
            if decimals is None:
                # Unknown decimals reported as base units rather than assumed to
                # be eighteen. A six-decimal balance shown with eighteen is a
                # trillion times too small, which reads as dust and gets skipped.
                decimals = 0
            contract = row.get("contractAddress")
            out.append(
                Transfer(
                    chain=chain,
                    tx=TxRef(chain, str(row["hash"]).lower()),
                    sender=self._addr(sender),
                    recipient=self._addr(recipient),
                    amount=Amount(
                        self._int(row.get("value")) or 0,
                        decimals,
                        str(row.get("tokenSymbol") or ""),
                    ),
                    kind=TransferKind.TOKEN,
                    timestamp=self._when(row),
                    block=self._int(row.get("blockNumber")),
                    index=self._int(row.get("logIndex")) or 0,
                    asset=self._addr(contract),
                )
            )
        if len(out) >= limit:
            raise ResultTruncated(
                f"tokentx filled the {limit}-row limit. Narrow the range; any "
                f"total from this set is a lower bound."
            )
        return out

    def get_logs(
        self,
        chain: ChainId,
        *,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
    ) -> list[dict[str, Any]]:
        """Raw logs over a block range.

        The capability this provider is really here for. An enumerative log
        query is where a silently-incomplete answer does the most damage: the
        result is a *set*, so a missing element does not look like anything.
        """
        params: dict[str, Any] = {
            "fromBlock": from_block if isinstance(from_block, str) else int(from_block),
            "toBlock": to_block if isinstance(to_block, str) else int(to_block),
        }
        if address:
            # One address only: the endpoint takes a single value, and joining
            # a list would silently query the first or none of them.
            if isinstance(address, list):
                if len(address) != 1:
                    raise ProviderError(
                        "blockscout getLogs takes one address; query them separately "
                        "so a partial answer cannot look like a complete one"
                    )
                address = address[0]
            params["address"] = address
        for i, topic in enumerate(topics or []):
            if topic:
                params[f"topic{i}"] = topic

        rows = self._get("logs", "getLogs", **params)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
