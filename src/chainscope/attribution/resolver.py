"""Reconcile claims from many sources.

The subtle requirement here is not merging --- that is a few lines. It is the
distinction between these two situations:

    "Three sources answered. None of them knows this address."
    "Three sources were asked. The sanctions list was unreachable."

They produce identical-looking empty results, and treating them alike is how a
sanctioned address gets reported as unremarkable. :class:`Resolution` therefore
carries which sources actually answered, and :attr:`Resolution.reliable` is
false whenever one did not.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..core.attribution import Attribution, Category, Confidence, ResolvedEntity, merge
from ..core.chainid import ChainId
from .base import Source, SourceError

__all__ = ["Resolution", "Resolver"]


@dataclass(frozen=True, slots=True)
class Resolution:
    """The answer for one address, plus how much of the picture we actually got."""

    address: str
    entity: ResolvedEntity | None
    consulted: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    """``(source name, error)`` for sources that could not answer."""

    @property
    def reliable(self) -> bool:
        """Whether every source was reachable.

        Check this before concluding an address is clean. An unreliable empty
        result is not evidence of anything.
        """
        return not self.failed

    @property
    def found(self) -> bool:
        return self.entity is not None

    @property
    def label(self) -> str:
        if self.entity:
            return self.entity.label
        return "unknown" if self.reliable else "unknown (incomplete lookup)"

    @property
    def category(self) -> Category:
        return self.entity.category if self.entity else Category.UNKNOWN

    @property
    def confidence(self) -> Confidence:
        return self.entity.confidence if self.entity else Confidence.SPECULATIVE

    @property
    def is_sanctioned(self) -> bool | None:
        """``True``/``False``, or ``None`` when the answer cannot be trusted.

        Three-valued on purpose. A sanctions question answered from an
        incomplete lookup deserves "I do not know", not "no".
        """
        if self.entity and self.entity.is_sanctioned:
            return True
        return False if self.reliable else None

    def warnings(self) -> list[str]:
        out = [f"source {name} failed: {err}" for name, err in self.failed]
        if self.entity and self.entity.disputed:
            cats = sorted({c.category.value for c in self.entity.all_claims})
            out.append(f"sources disagree on category: {', '.join(cats)}")
        if self.entity and not self.entity.confidence.is_actionable:
            out.append(
                f"best available claim is {self.entity.confidence.name} confidence "
                f"({self.entity.primary.method.value}); treat as a lead, not a finding"
            )
        return out

    def __str__(self) -> str:
        base = str(self.entity) if self.entity else self.label
        return f"{self.address}  {base}"


@dataclass
class Resolver:
    """Queries every configured source and merges the results."""

    sources: list[Source] = field(default_factory=list)
    _cache: dict[tuple[str, str | None], Resolution] = field(default_factory=dict, repr=False)

    def add(self, source: Source) -> Resolver:
        self.sources.append(source)
        # Offline sources first: they are fast, free, and cannot fail in the
        # way that turns a missing answer into a misleading one.
        self.sources.sort(key=lambda s: not s.offline)
        return self

    def resolve(self, address: str, chain: ChainId | None = None) -> Resolution:
        key = (address.lower(), str(chain) if chain else None)
        if (hit := self._cache.get(key)) is not None:
            return hit

        claims: list[Attribution] = []
        consulted: list[str] = []
        failed: list[tuple[str, str]] = []

        for source in self.sources:
            if not source.ready():
                failed.append((source.name, "not ready (missing data?)"))
                continue
            try:
                claims.extend(source.lookup(address, chain))
                consulted.append(source.name)
            except SourceError as exc:
                failed.append((source.name, str(exc)))
            except Exception as exc:
                # take down the whole lookup, but it must be visible.
                failed.append((source.name, f"{type(exc).__name__}: {exc}"))

        res = Resolution(
            address=address,
            entity=merge(claims),
            consulted=tuple(consulted),
            failed=tuple(failed),
        )
        self._cache[key] = res
        return res

    def resolve_many(
        self, addresses: Iterable[str], chain: ChainId | None = None
    ) -> dict[str, Resolution]:
        """Batch resolve, using each source's bulk path where it has one."""
        wanted = list(dict.fromkeys(addresses))
        pending = [
            a for a in wanted if (a.lower(), str(chain) if chain else None) not in self._cache
        ]

        gathered: dict[str, list[Attribution]] = {a: [] for a in pending}
        consulted: list[str] = []
        failed: list[tuple[str, str]] = []

        for source in self.sources:
            if not source.ready():
                failed.append((source.name, "not ready (missing data?)"))
                continue
            try:
                for addr, claims in source.lookup_many(pending, chain).items():
                    gathered.setdefault(addr, []).extend(claims)
                consulted.append(source.name)
            except SourceError as exc:
                failed.append((source.name, str(exc)))
            except Exception as exc:
                failed.append((source.name, f"{type(exc).__name__}: {exc}"))

        for addr in pending:
            key = (addr.lower(), str(chain) if chain else None)
            self._cache[key] = Resolution(
                address=addr,
                entity=merge(gathered.get(addr, [])),
                consulted=tuple(consulted),
                failed=tuple(failed),
            )

        return {a: self.resolve(a, chain) for a in wanted}

    # ---------------------------------------------------------------- helpers

    def screen(
        self, addresses: Iterable[str], chain: ChainId | None = None
    ) -> dict[str, Resolution]:
        """Return only addresses that are sanctioned or of unknown status.

        Deliberately includes the unknowns. A screening function that silently
        drops the addresses it could not check is worse than no screening
        function, because it produces a short, confident, incomplete list.
        """
        return {
            a: r
            for a, r in self.resolve_many(addresses, chain).items()
            if r.is_sanctioned is not False
        }

    def terminal(self, address: str, chain: ChainId | None = None) -> bool:
        """Whether tracing normally stops here (exchange, mixer, bridge, sanctions)."""
        r = self.resolve(address, chain)
        return bool(r.entity and r.entity.is_terminal)

    def citations(self) -> list[str]:
        """Citations for every configured source, for a report's methodology note."""
        return [f"{s.name}: {s.meta.citation()}" for s in self.sources]

    def report(self) -> list[dict[str, object]]:
        return [
            {
                "name": s.name,
                "offline": s.offline,
                "ready": s.ready(),
                "publisher": s.meta.publisher,
                "license": s.meta.license,
                "redistributable": s.meta.redistributable,
                "max_confidence": s.meta.max_confidence.name,
            }
            for s in self.sources
        ]

    def clear_cache(self) -> None:
        self._cache.clear()


def default_resolver(sources: Sequence[Source] = ()) -> Resolver:
    r = Resolver()
    for s in sources:
        r.add(s)
    return r
