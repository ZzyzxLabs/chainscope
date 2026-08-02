"""What an analyzer is.

Analyzers compute and return :class:`~chainscope.core.result.Result`. They do
not print, do not format, and do not decide what the user should conclude.

That constraint is worth more than it looks. Separating computation from
presentation is what makes the same analysis usable from a CLI, a report
generator, a JSON API, and a test that asserts on structure rather than parsing
stdout --- and it is what makes an analysis reproducible, because a Result
carries the parameters that produced it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..attribution.resolver import Resolver
from ..core.chainid import ChainId
from ..core.hypothesis import Hypothesis
from ..core.result import Evidence, Finding, Result
from ..providers.router import Router
from ..transport.audit import AuditLog

__all__ = ["Analyzer", "Context"]


@dataclass
class Context:
    """Everything an analyzer is allowed to reach.

    Passing this rather than letting analyzers construct their own providers
    keeps one cache, one rate limiter, and one audit log across a whole
    investigation --- which is what makes the audit log a complete record rather
    than a partial one.
    """

    chain: ChainId
    router: Router
    resolver: Resolver | None = None
    audit: AuditLog | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    """Caps on work: ``max_nodes``, ``max_depth``, ``per_node``.

    Analyzers must honour these and must record in ``Result.warnings`` when a
    limit truncated the work. Silent truncation is the failure mode here: a
    report that says "funds reached three exchanges" reads identically whether
    that was the answer or merely where the search stopped.
    """

    def limit(self, name: str, default: int) -> int:
        return int(self.limits.get(name, default))

    def evidence(self) -> Evidence:
        keys = self.audit.query_keys() if self.audit else ()
        return Evidence(query_keys=keys)


def history_of(
    ctx: Context,
    call: Callable[[Any], Iterable[Any]],
    *,
    capability: Any = None,
) -> tuple[list[Any], list[str]]:
    """An address history, and what is known about how complete it is.

    Returns ``(rows, warnings)``. The warnings are the point: an enumeration
    that came from one provider says so, and one where two providers disagreed
    says *that*, in the result rather than in a log nobody reads.

    Before this, seven of the nine analyzers went through `Router.dispatch` and
    got a bare list --- no record of which source answered and no way to tell a
    checked answer from an unchecked one. `Router.corroborate` existed and one
    analyzer called it. A capability that only the person who wrote it knows
    about is not a capability the tool has.
    """
    from ..providers.base import Capability

    found = ctx.router.enumerate(
        ctx.chain,
        capability or Capability.ADDRESS_HISTORY,
        call,
        key=lambda tx: getattr(getattr(tx, "ref", None), "hash", None) or repr(tx),
    )

    notes: list[str] = []
    if found.disagreed or not found.corroborated:
        notes.append(found.summary())
    for failure in found.failures:
        # A provider that could not answer is different from one that answered
        # with nothing, and only the second is evidence about the address.
        notes.append(f"a source could not be reached: {failure}")
    return found.rows, notes


class Analyzer(ABC):
    """Base class for analyses."""

    name: str = "unnamed"
    version: str = "1.0"
    description: str = ""

    def applicable(self, ctx: Context) -> bool:
        """Whether this analyzer can run against ``ctx``.

        Used by ``chainscope analyze --all`` to skip analyzers that need a
        capability or an ecosystem the context does not have, instead of
        producing a wall of identical errors.
        """
        return True

    @abstractmethod
    def run(self, ctx: Context, **params: Any) -> Result: ...

    # ---------------------------------------------------------------- helpers

    def _result(
        self,
        ctx: Context,
        *,
        findings: tuple[Finding, ...] = (),
        hypotheses: tuple[Hypothesis, ...] = (),
        warnings: tuple[str, ...] = (),
        params: dict[str, Any] | None = None,
        started: datetime | None = None,
    ) -> Result:
        # The context's own inputs belong in `params` alongside the analyzer's.
        # A result recorded without them cannot say which chain it ran on or
        # what limit truncated it --- and `max_nodes` is exactly the sort of cap
        # that turns "funds reached three exchanges" into "where the search
        # stopped". The caller's params win on a clash, since an analyzer that
        # deliberately records something under one of these names meant it.
        # Read defensively: this is a base helper and the information is
        # additive provenance, so a context that cannot supply it should record
        # nothing rather than fail. The validation suites drive analyzers with
        # deliberately minimal stand-ins --- one method each --- and making them
        # implement the whole of `Context` to gain a params key would trade a
        # real testing property for a bookkeeping one.
        merged: dict[str, Any] = {}
        chain = getattr(ctx, "chain", None)
        if chain is not None:
            merged["chain"] = str(chain)
        limits = getattr(ctx, "limits", None)
        if limits:
            merged["limits"] = dict(limits)
        merged.update(params or {})
        return Result(
            analyzer=self.name,
            version=self.version,
            findings=findings,
            hypotheses=hypotheses,
            evidence=ctx.evidence(),
            warnings=warnings,
            params=merged,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} v{self.version}>"
