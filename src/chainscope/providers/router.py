"""Pick a provider, then pick the next one when it fails.

Free public endpoints fail often and independently: an RPC node times out, an
explorer rate-limits, an indexer returns a 502. A tool that gives up on the
first failure is unusable against them; a tool that retries the same dead host
forever is worse.

The router turns "which source can answer this" into a ranked list and walks it,
so an analyzer never has to know that Etherscan can list address history but
public RPC cannot.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ..core.chainid import ChainId
from .base import Capability, Provider, ProviderError

__all__ = ["Corroboration", "NoProviderError", "Router"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Corroboration(Generic[T]):
    """The result of asking two providers the same enumerative question.

    Deliberately not a plain list. The whole value is in the parts a list
    cannot carry: *who* was asked, and *what only one of them saw*. Flattening
    that away would leave a caller unable to tell a corroborated answer from a
    single-source one, which is the distinction the type exists to preserve.
    """

    rows: list[T] = field(default_factory=list)
    """The union. Larger than either source when they disagree, on purpose ---
    for an enumeration, a row one source missed is still a row."""

    sources: tuple[str, ...] = ()
    only_in: dict[str, list[T]] = field(default_factory=dict)
    """Rows seen by exactly one provider, keyed by that provider's name."""

    failures: tuple[str, ...] = ()
    """Providers that could not answer. Distinct from providers that answered
    with nothing."""

    @property
    def corroborated(self) -> bool:
        """Whether two sources actually agreed.

        False both when only one source answered and when two disagreed. A
        caller that treats those the same is fine; one that reports "verified"
        must not, and this makes the difference impossible to miss."""
        return len(self.sources) > 1 and not self.only_in

    @property
    def disagreed(self) -> bool:
        return bool(self.only_in)

    def summary(self) -> str:
        if len(self.sources) < 2:
            asked = self.sources[0] if self.sources else "nothing"
            return (
                f"{len(self.rows)} rows from {asked} alone --- not corroborated. "
                f"A second independent source is what turns a silently short "
                f"answer into a visible disagreement."
            )
        if not self.only_in:
            return f"{len(self.rows)} rows, agreed by {' and '.join(self.sources)}."
        parts = [f"{name} alone saw {len(rows)}" for name, rows in sorted(self.only_in.items())]
        return (
            f"{len(self.rows)} rows in union, but {'; '.join(parts)}. "
            f"Which source is right is not decidable from here: the larger "
            f"result is usually the complete one, but a provider double-counting "
            f"also produces more rows. Check the differing rows directly."
        )


class NoProviderError(ProviderError):
    """Nothing configured can answer this.

    The message names the capability and the chain, because the fix is almost
    always "add an API key" and the user needs to know which one.
    """


class Router:
    """Capability-based dispatch across a set of providers."""

    def __init__(
        self,
        providers: Sequence[Provider] = (),
        *,
        preferred: Sequence[str] = (),
    ) -> None:
        self._providers: list[Provider] = list(providers)
        self.preferred = list(preferred)
        """Provider names to try first, in order, regardless of cost tier."""

    def add(self, provider: Provider) -> None:
        self._providers.append(provider)

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers)

    def candidates(self, chain: ChainId, capability: Capability) -> list[Provider]:
        """Providers that can serve this, best first.

        Order: explicitly preferred, then cheapest tier, then declaration order.
        Declaration order last means a user who lists two equivalent providers
        gets deterministic behaviour rather than dict ordering.
        """
        able = [p for p in self._providers if p.supports(chain, capability)]

        def rank(p: Provider) -> tuple[int, int, int]:
            pref = (
                self.preferred.index(p.name)
                if p.name in self.preferred
                else len(self.preferred)
            )
            return (pref, int(p.cost), self._providers.index(p))

        return sorted(able, key=rank)

    def dispatch(
        self,
        chain: ChainId,
        capability: Capability,
        call: Callable[[Provider], T],
    ) -> T:
        """Run ``call`` against the best provider, falling back on failure.

        Only :class:`ProviderError` triggers a fallback. A ``ValueError`` from
        parsing means the provider answered and our code is wrong; retrying
        elsewhere would hide the bug behind a different provider's response.
        """
        options = self.candidates(chain, capability)
        if not options:
            raise NoProviderError(self._explain(chain, capability))

        failures: list[str] = []
        for provider in options:
            try:
                return call(provider)
            except ProviderError as exc:
                failures.append(f"{provider.name}: {exc}")
            except Exception as exc:
                failures.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                if len(options) == 1:
                    raise

        raise ProviderError(
            f"all {len(options)} providers failed for "
            f"{capability.name} on {chain}:\n  " + "\n  ".join(failures)
        )

    def corroborate(
        self,
        chain: ChainId,
        capability: Capability,
        call: Callable[[Provider], Iterable[T]],
        *,
        key: Callable[[T], Hashable],
        required: bool = False,
    ) -> Corroboration[T]:
        """Run an enumerative query against two independent providers.

        The failure this exists for is not a provider erroring --- ``dispatch``
        already handles that, and an error at least announces itself. It is a
        provider returning ``200 OK`` with a *short* answer: no error envelope,
        no truncation marker, nothing to notice. Field notes from a real trace
        record one archive endpoint's ``eth_getLogs`` returning a log when asked
        for a single block and losing it when asked in five-hundred-block
        ranges. One withdrawal address of thirteen went missing that way, and
        nothing anywhere said so.

        Every existing guard in this package assumes the provider tells you
        something is wrong. :func:`~chainscope.providers.etherscan.is_cacheable`
        reads an error envelope; ``ResultTruncated`` reads a documented cap.
        Neither fires here, because from the outside a short list and a complete
        list are the same shape. **The only way to detect it is to ask twice.**

        Returns both the union and the disagreement rather than picking a
        winner. Which source is right is not knowable from here --- the larger
        result is usually the complete one, but a provider double-counting an
        internal transaction also produces more rows --- and a function that
        silently chose would be reproducing the problem one level up.

        ``required=False`` means a lone provider is not an error: the query
        still runs, and ``sources`` says it was one. That distinction has to
        survive to the caller, because "corroborated by two" and "asked one" are
        different claims and only one of them is worth the name.
        """
        options = self.candidates(chain, capability)
        if not options:
            raise NoProviderError(self._explain(chain, capability))
        if required and len(options) < 2:
            raise NoProviderError(
                f"corroboration needs two independent providers for "
                f"{capability.name} on {chain}; only {options[0].name} is "
                f"configured. Add a second, or accept a single-source answer."
            )

        seen: dict[Hashable, T] = {}
        by_source: dict[str, set[Hashable]] = {}
        failures: list[str] = []

        for provider in options[:2]:
            try:
                rows = list(call(provider))
            except ProviderError as exc:
                failures.append(f"{provider.name}: {exc}")
                continue
            keys = set()
            for row in rows:
                k = key(row)
                keys.add(k)
                # First sighting wins. The point here is set membership, not
                # reconciling two renderings of one row.
                seen.setdefault(k, row)
            by_source[provider.name] = keys

        if not by_source:
            raise ProviderError(
                f"every provider failed for {capability.name} on {chain}:\n  "
                + "\n  ".join(failures)
            )

        only: dict[str, list[T]] = {}
        if len(by_source) > 1:
            for name, keys in by_source.items():
                others: set[Hashable] = set()
                for other, other_keys in by_source.items():
                    if other != name:
                        others |= other_keys
                missing = keys - others
                if missing:
                    only[name] = [seen[k] for k in missing]

        return Corroboration(
            rows=list(seen.values()),
            sources=tuple(by_source),
            only_in=only,
            failures=tuple(failures),
        )

    def _explain(self, chain: ChainId, capability: Capability) -> str:
        on_chain = [p.name for p in self._providers if chain in p.chains]
        msg = f"no provider offers {capability.name} on {chain}."
        if on_chain:
            msg += f" Providers configured for this chain: {', '.join(on_chain)}."
        else:
            msg += " No provider is configured for this chain at all."
        if capability is Capability.ADDRESS_HISTORY:
            msg += (
                " Listing an address's transactions needs an explorer-class "
                "provider; plain RPC cannot do it."
            )
        elif capability is Capability.ARCHIVE_STATE:
            msg += (
                " Historical state needs an archive node; most free endpoints "
                "keep only ~128 blocks."
            )
        return msg

    def capabilities_for(self, chain: ChainId) -> Capability:
        """Everything currently reachable on a chain. Useful for diagnostics."""
        out = Capability.NONE
        for p in self._providers:
            if chain in p.chains:
                out |= p.capabilities
        return out

    def report(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "cost": p.cost.name,
                "chains": sorted(str(c) for c in p.chains),
                "capabilities": [
                    c.name
                    for c in Capability
                    if c is not Capability.NONE and p.capabilities & c
                ],
            }
            for p in self._providers
        ]

    def __len__(self) -> int:
        return len(self._providers)
