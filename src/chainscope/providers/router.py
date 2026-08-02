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

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from ..core.chainid import ChainId
from .base import Capability, Provider, ProviderError

__all__ = ["NoProviderError", "Router"]

T = TypeVar("T")


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
