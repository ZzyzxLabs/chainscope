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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
        corroborate_enumerations: bool = True,
    ) -> None:
        self._providers: list[Provider] = list(providers)
        self.preferred = list(preferred)
        """Provider names to try first, in order, regardless of cost tier."""

        self.corroborate_enumerations = corroborate_enumerations
        """Whether :meth:`enumerate` asks a second source by default.

        On, because the failure this project was built around is a query whose
        answer is a *set* coming back silently short --- an archive endpoint
        returning twelve of thirteen logs with HTTP 200, and one withdrawal
        address going missing from a real case. A second independent source is
        what turns that from an invisible wrong answer into a visible
        disagreement.

        It costs a second request per enumerating call. That is the trade being
        made deliberately: latency and rate limit against a class of error
        nothing downstream can detect. `--single-source` opts out, and the
        result still says which it was.
        """

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

    def enumerate(
        self,
        chain: ChainId,
        capability: Capability,
        call: Callable[[Provider], Iterable[T]],
        *,
        key: Callable[[T], Hashable],
    ) -> Corroboration[T]:
        """Ask for something whose answer is a *set*, and say how sure that is.

        The point is not that this always corroborates --- it cannot, when only
        one provider serves the chain. The point is that **the answer always
        carries a statement about its own completeness**, so a single-source
        result is never mistaken for a checked one.

        That was the gap. `corroborate` existed and exactly one of nine
        analyzers called it; the other seven went through `dispatch` and
        returned a bare list that said nothing about where it came from or
        whether anyone had checked it. An investigator who did not know the
        feature existed got the weaker answer and no sign of it.
        """
        options = self.candidates(chain, capability)
        if self.corroborate_enumerations and len(options) > 1:
            return self.corroborate(chain, capability, call, key=key)

        # One source, either because that is all there is or because the caller
        # asked for one. Returned in the same shape, so callers have a single
        # path and `summary()` says which case this was.
        rows = list(self.dispatch(chain, capability, call))
        return Corroboration(rows=rows, sources=(options[0].name,) if options else ())

    def _ask_concurrently(
        self,
        options: Sequence[Provider],
        call: Callable[[Provider], Iterable[T]],
        *,
        want: int,
    ) -> list[tuple[Provider, list[T], str | None]]:
        """Ask providers in parallel, stopping once ``want`` have succeeded.

        **It issues exactly the requests the serial version would.** That is the
        constraint the design is built around, not an accident: candidates are
        ranked by cost tier, so a naive fan-out across every option would bill a
        paid provider to corroborate an answer two free ones had already agreed
        on, and would spend rate limit nobody asked to spend.

        The way it holds is to keep ``want`` requests in flight and top up only
        when one *fails*. Serially, a provider is tried only because the ones
        before it did not yield enough successes; here a provider is launched
        only when a launched one has already failed. Both stop at ``want``
        successes, so both make the same calls --- these are merely overlapped.

        Returns ``(provider, rows, failure)`` in **``options`` order**, not
        completion order, so the caller's merge cannot depend on which endpoint
        happened to be quick.

        Threads rather than asyncio: every provider is a blocking HTTP client,
        the shared state they touch (cache, throttle, circuit breaker, audit
        log) is already lock-guarded, and the token bucket computes its wait
        under the lock and sleeps outside it --- so the configured per-host rate
        is still enforced across all of them. Making this async would mean
        rewriting the transport for a path whose cost is entirely latency.
        """
        if not options:
            return []
        # One candidate is the common case on most chains. Skipping the pool
        # keeps the stack trace and the audit log identical to the serial path.
        if len(options) == 1 or want <= 1:
            provider = options[0]
            return [(provider, *self._attempt(provider, call))]

        outcomes: dict[int, tuple[list[T], str | None]] = {}
        succeeded = 0
        next_index = 0
        with ThreadPoolExecutor(max_workers=want, thread_name_prefix="chainscope-ask") as pool:
            running: dict[Any, int] = {}
            while next_index < min(want, len(options)):
                job = pool.submit(self._attempt, options[next_index], call)
                running[job] = next_index
                next_index += 1

            while running:
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for future in done:
                    index = running.pop(future)
                    rows, failure = future.result()
                    outcomes[index] = (rows, failure)
                    if failure is None:
                        succeeded += 1
                    elif succeeded + len(running) < want and next_index < len(options):
                        # Top up only on a failure, and only if the ones still
                        # in flight cannot reach `want` on their own.
                        job = pool.submit(self._attempt, options[next_index], call)
                        running[job] = next_index
                        next_index += 1

        return [(options[i], outcomes[i][0], outcomes[i][1]) for i in sorted(outcomes)]

    @staticmethod
    def _attempt(
        provider: Provider, call: Callable[[Provider], Iterable[T]]
    ) -> tuple[list[T], str | None]:
        """Run one provider, returning its rows or a description of its failure.

        Never raises. A provider that throws inside a worker thread would
        otherwise surface as an unrelated traceback from `future.result()` at a
        point that says nothing about which provider failed.
        """
        try:
            return list(call(provider)), None
        except ProviderError as exc:
            return [], f"{provider.name}: {exc}"
        except Exception as exc:
            # Same shape as dispatch: a parsing bug in one provider's adapter
            # should not take down a query another can answer. Recorded by type
            # so it is not mistaken for an upstream error.
            return [], f"{provider.name}: {type(exc).__name__}: {exc}"

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

        # Walk past a failure rather than stopping at the first two candidates.
        #
        # Truncating to options[:2] meant one failing provider left a
        # single-source answer even when a third could have corroborated it ---
        # and free public endpoints fail constantly, which is the reason the
        # router falls back at all. Two *successes* is the target, not two
        # attempts.
        #
        # Run concurrently, because the two providers are independent and the
        # wait is the network. Measured: an uncached round trip is ~1.6s and the
        # local half of this runs at 2,451x that, so asking twice in sequence
        # spends its entire budget waiting twice for answers that never needed
        # to be ordered.
        results = self._ask_concurrently(options, call, want=2)

        # Merged in `options` order, never completion order. Whichever provider
        # answers first is a fact about the network that day, and `setdefault`
        # below means it would decide which rendering of a duplicated row is
        # kept --- so replaying the same case on a slower connection could
        # produce a different result. Ordering by preference keeps the answer a
        # function of the inputs.
        for provider, rows, failure in results:
            if len(by_source) >= 2:
                break
            if failure is not None:
                failures.append(failure)
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
