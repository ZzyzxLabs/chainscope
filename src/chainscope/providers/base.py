"""What a data source is, and what it can do.

Providers are modelled by **capability**, not by chain. The reason is that one
chain has sources with genuinely different powers: a public RPC endpoint cannot
list an address's transaction history at all, an explorer API can but cannot
trace execution, and an archive provider can answer historical state queries
that both of the others refuse.

Modelling by chain would push that knowledge into every analyzer. Modelling by
capability means adding one API key makes a capability available everywhere that
provider reaches, with no analyzer changes --- which is where the flexibility in
this design actually comes from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Flag, IntEnum, auto
from typing import Any

from ..core.chainid import ChainId
from ..core.models import Account, Block, Transaction, Transfer
from ..transport.http import Client

__all__ = ["Capability", "CostTier", "Provider", "ProviderError", "ResultTruncated"]


class ProviderError(RuntimeError):
    """A provider could not answer. The router will try the next one."""


class ResultTruncated(ProviderError):
    """The source returned its maximum and there is certainly more.

    A distinct type because callers must handle it differently from a plain
    failure: the data is usable but incomplete, and any total derived from it is
    a lower bound. Returning the rows silently produces a confident wrong number.

    A :class:`ProviderError` subclass on purpose, so the router treats it as
    "try the next source" rather than as fatal. Rebasing it on RuntimeError
    stopped ``dispatch`` falling back and stopped ``corroborate`` degrading to
    one source --- one capped page would have aborted the whole query.

    It lives here rather than in one provider because *every* enumerating
    provider owes the caller this distinction. One that slices to the limit and
    says nothing is the exact failure :meth:`Router.corroborate` exists to
    catch, and a corroboration source that truncates silently poisons the
    mechanism built to detect truncation.

    **It carries the rows it found.** Refusing to return them makes the signal
    unusable by the one caller who can act on it: a pager, for whom a full page
    is the expected outcome rather than a failure. Discarding them forced that
    caller to either re-request the same page through a different path or give
    up --- and giving up is what a page in a browser was doing, telling the user
    to go and run a CLI command.

    A caller that ignores ``rows`` is unaffected: the exception still
    propagates, and a total built from it is still a lower bound. What changes
    is that completing the set is now possible without another round trip.
    """

    def __init__(
        self,
        message: str,
        rows: list[Any] | None = None,
        *,
        window_short: bool = False,
    ) -> None:
        super().__init__(message)
        self.rows = rows or []
        self.window_short = window_short
        """Whether the *range asked for* was not covered, as opposed to a full
        page with more behind it.

        The two need separating because a pager treats a short page as the end
        of the data --- correctly --- and a short *window* is not that. A
        log-scanning provider that reads the last 120,000 blocks of a
        million-block history returns three rows, and three is fewer than a
        page, so the pager concluded the read was complete. It was complete of
        the window and silent about the rest, which is the one thing this
        package exists to refuse."""


class Capability(Flag):
    """What a provider can answer.

    Declare these honestly. Overstating is worse than omitting: the router will
    select you, the call returns partial data, and an analyzer draws a
    conclusion from an incomplete picture. A missing capability degrades
    gracefully; a lying one produces wrong answers that nothing downstream can
    detect.
    """

    NONE = 0

    BLOCK = auto()
    TRANSACTION = auto()
    RECEIPT = auto()
    LOGS = auto()
    BALANCE = auto()

    ADDRESS_HISTORY = auto()
    """List every transaction an address sent or received.

    Explorer-class. Plain RPC cannot do this at any price, which surprises
    people often enough to be worth stating."""

    ASSET_TRANSFERS = auto()
    """Native, token, and internal transfers in one paginated call.

    Indexer-class. Worth a great deal in practice: internal transfers are where
    swap proceeds and withdrawal payouts live, and reconstructing them from
    traces is slow and error-prone."""

    TOKEN_TRANSFERS = auto()
    """ERC-20 ``Transfer`` logs for an address. **Tokens only.**

    Deliberately not `ASSET_TRANSFERS`, which promises native and internal
    movement as well. A log scan cannot see either: a plain value send emits no
    event, and a contract-to-contract call emits nothing either --- so an
    address that only ever moved BNB comes back from this empty, which looks
    exactly like an address that never moved anything.

    Declared separately so the router keeps preferring a real indexer, and so a
    caller that falls back to this knows to say what it did not look at. That
    sentence is the whole reason the capability is split rather than merged:
    the tempting version of this change was to add ASSET_TRANSFERS to the
    JSON-RPC provider, and it would have quietly converted "nobody indexes this
    chain" into "this address has no history".

    Reachable from any endpoint serving ``eth_getLogs``, which is every EVM RPC
    --- and that is the point. Before this, a chain with no explorer key was a
    chain the tool could say nothing about at all.
    """

    ARCHIVE_STATE = auto()
    """State at an arbitrary historical block.

    Most free endpoints keep roughly 128 blocks of state, so any question of the
    form "what did this contract hold at the time" needs this."""

    TRACE = auto()
    CONTRACT_SOURCE = auto()
    UTXO_SET = auto()
    TOKEN_METADATA = auto()

    def covers(self, needed: Capability) -> bool:
        return (self & needed) == needed


class CostTier(IntEnum):
    """Preference order when several providers can serve a request.

    Free public endpoints first: an investigation that burns a paid quota on
    queries a public node would have answered is wasting the budget it will need
    for the archive lookups only paid tiers can serve.
    """

    FREE_PUBLIC = 0
    FREE_KEYED = 1
    PAID = 2
    LOCAL = -1
    """A node you run. Fastest, most private, always preferred."""


class Provider:
    """Base class for data sources.

    Deliberately concrete rather than abstract. Subclasses declare ``name``,
    ``chains``, ``capabilities``, and ``cost``, then implement only the methods
    matching what they declared; everything else inherits a default that raises
    :class:`ProviderError`, which the router reads as "try the next one".

    Making these abstract would force every provider to write a dozen stubs for
    capabilities it never claimed --- and a stub that returns an empty list is
    exactly the failure this design avoids.
    """

    name: str = "unnamed"
    chains: frozenset[ChainId] = frozenset()
    capabilities: Capability = Capability.NONE
    cost: CostTier = CostTier.FREE_PUBLIC

    def __init__(self, client: Client | None = None) -> None:
        self.client = client or Client()

    # ---------------------------------------------------------------- routing

    #: CAIP-2 namespaces this provider can serve, as a class-level fact.
    #:
    #: Declared separately from ``chains`` because that is an instance detail
    #: --- which networks *this* configured provider was pointed at --- while
    #: the namespace is a property of the provider itself: an Etherscan client
    #: cannot serve Bitcoin however it is configured.
    #:
    #: Discovery needs the static version. `chainscope doctor` inspects entry
    #: points without constructing anything, and before this existed it could
    #: not tell that a Sui provider offering ADDRESS_HISTORY said nothing about
    #: whether that question was answerable on Ethereum --- so it reported the
    #: capability as reachable for every chain.
    ecosystems: frozenset[str] = frozenset()

    @classmethod
    def serves(cls, chain: ChainId) -> bool:
        """Whether this provider *could* serve ``chain``, uninstantiated.

        Empty ``ecosystems`` means unstated, which is treated as yes: a
        third-party provider that has not declared one should not silently
        vanish from a capability report.
        """
        return not cls.ecosystems or chain.namespace in cls.ecosystems

    @classmethod
    def from_settings(cls, settings: Any, chain: ChainId, client: Any = None) -> list[Provider]:
        """Build whatever instances of this provider ``settings`` supports.

        ``client`` is the run's shared :class:`~chainscope.transport.http.Client`
        and should be passed straight through. Letting each provider make its
        own meant two rate limiters against one upstream quota and an audit log
        holding half the queries --- and that log is what
        ``Context.evidence()`` reads to say what a conclusion was built from.

        Returns a list because one provider class can yield several configured
        instances --- an RPC provider produces one per endpoint --- and an empty
        list because "no key for this one" is the ordinary case, not an error.

        The default returns nothing. A provider that does not implement this is
        usable from Python and invisible to the CLI, which is the safe direction:
        the alternative is guessing at a constructor and handing the router
        something misconfigured.

        This exists because for a long time nothing built a populated router at
        all. ``chainscope analyze`` passed an empty one, so every analyzer
        needing a capability reported that no provider offered it --- while
        ``doctor``, which reads entry points directly, listed the same
        capabilities as available. Both were describing something real; neither
        was describing the same thing.
        """
        return []

    def supports(self, chain: ChainId, capability: Capability) -> bool:
        return chain in self.chains and self.capabilities.covers(capability)

    def healthy(self) -> bool:
        """Cheap liveness signal. Override if the provider can check itself."""
        return True

    # ---------------------------------------------------------------- queries
    #
    # Default implementations refuse rather than return empty. An empty list
    # from an unimplemented method is indistinguishable from "this address has
    # no history", and that ambiguity has produced silently wrong analyses.

    def _unsupported(self, what: str) -> ProviderError:
        return ProviderError(f"{self.name} does not provide {what}")

    def get_block(self, chain: ChainId, number: int | str) -> Block:
        raise self._unsupported("blocks")

    def get_transaction(self, chain: ChainId, tx_hash: str) -> Transaction:
        raise self._unsupported("transactions")

    def get_account(self, chain: ChainId, address: str) -> Account:
        raise self._unsupported("accounts")

    def get_logs(
        self,
        chain: ChainId,
        *,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
    ) -> list[dict[str, Any]]:
        raise self._unsupported("logs")

    def address_history(
        self,
        chain: ChainId,
        address: str,
        *,
        start_block: int = 0,
        end_block: int | str = "latest",
        limit: int = 1000,
    ) -> list[Transaction]:
        raise self._unsupported("address history")

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
        """One page of the value movements touching an address.

        ``page`` is 1-based and exists because :class:`ResultTruncated` is the
        honest answer to a busy address and an unusable one on its own: the
        caller is told the set is incomplete and given no way to complete it.
        Range narrowing is not that way --- the truncation signal is an
        exception, so it carries no cursor, and bisecting blind spends its
        budget halving empty space above the chain head.

        A provider that cannot page should ignore this and keep returning the
        first page. Returning a *short* page is how a caller knows to stop, so
        a provider that silently repeats page one will loop until the caller's
        own budget stops it --- which is why the caller must bound itself
        rather than trust termination.
        """
        raise self._unsupported("asset transfers")

    def block_at_time(self, chain: ChainId, when: datetime) -> Block:
        raise self._unsupported("time-to-block lookup")

    def __repr__(self) -> str:
        caps = [
            c.name or "?"
            for c in Capability
            if c is not Capability.NONE and self.capabilities & c
        ]
        return f"<{type(self).__name__} {self.name} [{', '.join(caps)}]>"


class ReadOnlyProvider(Provider, ABC):
    """Marker for providers that physically cannot write.

    Every provider in this repository is one. The class exists so a third-party
    provider must *opt out* of the guarantee explicitly, rather than quietly
    lacking it.
    """

    @property
    @abstractmethod
    def endpoint(self) -> str: ...
