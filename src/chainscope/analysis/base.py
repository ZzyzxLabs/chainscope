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
        return Result(
            analyzer=self.name,
            version=self.version,
            findings=findings,
            hypotheses=hypotheses,
            evidence=ctx.evidence(),
            warnings=warnings,
            params=params or {},
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} v{self.version}>"
