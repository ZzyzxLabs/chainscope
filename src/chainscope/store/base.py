"""The store: what is connected to what.

Distinct from the cache, and conflating them is expensive. The cache is keyed by
query and answers *"what did this request return"*. It cannot answer *"every
address that has ever sent to X"*, because it has no idea what is inside the
responses it holds --- and that second question is what every traversal, filter,
and alert is made of.

**The rule that keeps this safe: a store must be fully rebuildable from the
cache.** It is a derived index, never a source of truth. Delete it and it can be
reconstructed offline from recorded responses.

That constraint pays for itself three times: a schema change becomes a rebuild
rather than a re-crawl, a half-written store cannot silently poison an analysis,
and a case bundle can ship a whole database rather than a single conclusion.

The direction matters and is easy to get backwards: **the store may be derived
from the cache; the cache may never be derived from the store.** A response
reconstructed from parsed entities is not evidence of what a provider said.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.attribution import Attribution
from ..core.chainid import ChainId
from ..core.models import Transfer

__all__ = ["EdgeSummary", "Query", "Store", "StoreError", "StoreStats"]

#: Bumped when the on-disk shape changes. A store carrying an older version is
#: migrated or rebuilt; it is never read optimistically.
SCHEMA_VERSION = 2


class StoreError(RuntimeError):
    """The store cannot be read, written, or migrated."""


@dataclass(frozen=True, slots=True)
class Query:
    """A filter over stored transfers.

    Deliberately a value object rather than a string DSL. A dataclass is
    introspectable, serialisable into `Result.params`, and cannot contain
    injection --- and a saved query that round-trips through a case bundle has
    to be data, not syntax.
    """

    chain: ChainId | None = None
    sender: str | None = None
    recipient: str | None = None
    address: str | None = None
    """Either side. Mutually exclusive with sender/recipient in practice."""

    asset: str | None = None
    min_amount: int | None = None
    """Raw units. Comparing raw integers avoids re-deriving decimals per row."""

    max_amount: int | None = None
    after: datetime | None = None
    before: datetime | None = None
    min_block: int | None = None
    max_block: int | None = None
    kinds: tuple[str, ...] = ()
    limit: int = 1000
    offset: int = 0
    order: str = "time"
    """``time``, ``amount``, or ``block``."""

    def describe(self) -> str:
        parts = [
            f"{k}={v}"
            for k, v in (
                ("chain", self.chain),
                ("from", self.sender),
                ("to", self.recipient),
                ("address", self.address),
                ("asset", self.asset),
                ("min", self.min_amount),
                ("max", self.max_amount),
                ("after", self.after),
                ("before", self.before),
                ("min_block", self.min_block),
                ("max_block", self.max_block),
                ("kinds", ",".join(self.kinds) or None),
            )
            if v is not None
        ]
        return " ".join(parts) or "everything"


@dataclass(frozen=True, slots=True)
class EdgeSummary:
    """Aggregate flow between two addresses.

    The unit graph traversal works in. Rolling transfers up here rather than in
    each analyzer means one address pair with 400 transfers costs one row, which
    is the difference between a traversal that finishes and one that does not.
    """

    sender: str
    recipient: str
    asset: str | None
    """Contract address, or ``None`` for the chain's native asset. This is the
    identity; :attr:`symbol` is only what to print."""

    total_raw: int
    transfer_count: int

    symbol: str = ""
    decimals: int = 18
    """Without these a renderer has nothing to print an amount *with*, and the
    two defaults it would otherwise invent --- the contract address as a name,
    and eighteen decimals --- are wrong for every native edge and every
    six-decimal token respectively. USDC displayed at eighteen is a trillion
    times too small."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class StoreStats:
    transfers: int = 0
    addresses: int = 0
    attributions: int = 0
    chains: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    bytes: int = 0
    rebuildable: bool = True
    """Whether every stored row can be regenerated from the recorded cache.

    False means someone wrote directly to the store, and §4.8's guarantee no
    longer holds for this case.
    """


class Store(ABC):
    """Persistent, rebuildable index over normalised entities."""

    name: str = "unnamed"
    protocol_version: int = 1
    """Independent of the package version. The loader refuses a plugin built
    against an incompatible protocol rather than failing later inside it."""

    # ---------------------------------------------------------------- writing

    @abstractmethod
    def put_transfers(self, transfers: Iterable[Transfer], *, source: str = "") -> int:
        """Insert transfers idempotently.

        Must be safe to call repeatedly with overlapping data: ingestion is
        demand-driven and the same range gets re-walked constantly. Returns the
        number newly inserted.
        """

    @abstractmethod
    def put_attributions(self, attributions: Iterable[Attribution]) -> int:
        """Record attribution claims. Never destructive --- conflicting claims
        about one address coexist, exactly as in the resolver."""

    @abstractmethod
    def mark_expanded(self, address: str, chain: ChainId, *, depth: int = 0) -> None:
        """Record that an address's neighbourhood was fetched.

        Distinguishes the frontier from the interior. Without it, an address
        that was seen but never followed looks identical to one that was
        followed and had nothing --- and that ambiguity turns a stopped
        traversal into an apparent conclusion.
        """

    @abstractmethod
    def is_expanded(self, address: str, chain: ChainId) -> bool: ...

    # ---------------------------------------------------------------- reading

    @abstractmethod
    def transfers(self, query: Query) -> Iterator[Transfer]: ...

    @abstractmethod
    def count(self, query: Query) -> int: ...

    @abstractmethod
    def edges(
        self,
        address: str,
        chain: ChainId,
        *,
        direction: str = "out",
        min_total: int = 0,
    ) -> list[EdgeSummary]: ...

    @abstractmethod
    def attributions(self, address: str) -> list[Attribution]: ...

    @abstractmethod
    def frontier(self, chain: ChainId) -> list[str]:
        """Addresses seen but never expanded.

        The honest boundary of a case. A report that does not distinguish this
        from "nothing further was found" is overstating its coverage.
        """

    # ---------------------------------------------------------------- lifecycle

    @abstractmethod
    def stats(self) -> StoreStats: ...

    @abstractmethod
    def clear(self) -> None:
        """Drop everything. Safe by design --- see the rebuild guarantee."""

    def migrate(self, from_version: int) -> None:
        """Upgrade an older store.

        Must fail loudly rather than half-apply: a partially migrated store is
        indistinguishable from a complete one and will be trusted.
        """
        raise StoreError(
            f"{self.name} cannot migrate from schema {from_version} to "
            f"{SCHEMA_VERSION}. Rebuild from the cache instead."
        )

    def close(self) -> None:
        return None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} v{self.protocol_version}>"


def rebuild_from_cache(store: Store, records: Iterable[dict[str, Any]]) -> int:
    """Reconstruct a store from recorded provider responses.

    The escape hatch §4.8 promises. Intentionally takes an iterable of decoded
    records rather than reaching into the cache itself, so a bundle, a live
    cache, and a test fixture all replay through the same path.
    """
    raise NotImplementedError("rebuild is implemented per chain adapter; see chains/*.replay")
