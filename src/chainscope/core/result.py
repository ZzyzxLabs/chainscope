"""What an analyzer returns.

Analyzers compute; they do not print. Separating the two buys four things from
one decision: machine-readable output, report generation, an audit trail, and
tests that assert on structure instead of parsing stdout.

``Result.params`` is load-bearing. A result that cannot be reproduced from its
own parameters is an anecdote --- fine for a hunch, useless the moment anyone
asks how you got there.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .hypothesis import Hypothesis

__all__ = ["Evidence", "Finding", "Result", "Severity"]


class Severity(str, Enum):
    """How much attention a finding warrants. Not how certain it is --- that is
    the finding's ``confidence``, and conflating the two produces alarming
    reports about things nobody verified."""

    INFO = "info"
    NOTABLE = "notable"
    IMPORTANT = "important"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Evidence:
    """The queries behind a conclusion.

    Cache keys rather than payloads: paired with the content-addressed cache,
    they let someone else replay the exact requests offline. Copying the
    responses in here instead would make every Result enormous and still not
    prove where the data came from.
    """

    query_keys: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def merged(self, other: Evidence) -> Evidence:
        return Evidence(
            query_keys=tuple(dict.fromkeys(self.query_keys + other.query_keys)),
            sources=tuple(dict.fromkeys(self.sources + other.sources)),
            notes=self.notes + other.notes,
        )

    def __bool__(self) -> bool:
        return bool(self.query_keys or self.sources)


@dataclass(frozen=True, slots=True)
class Finding:
    """A single structured conclusion."""

    title: str
    severity: Severity = Severity.INFO
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence = field(default_factory=Evidence)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a finding needs a title")

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.title}"


@dataclass(frozen=True, slots=True)
class Result:
    """The output of one analyzer run."""

    analyzer: str
    findings: tuple[Finding, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    evidence: Evidence = field(default_factory=Evidence)
    warnings: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    version: str = "1.0"

    @property
    def duration(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def is_empty(self) -> bool:
        return not self.findings and not self.hypotheses

    def by_severity(self, minimum: Severity) -> tuple[Finding, ...]:
        order = list(Severity)
        floor = order.index(minimum)
        return tuple(f for f in self.findings if order.index(f.severity) >= floor)

    @property
    def top(self) -> Hypothesis | None:
        """Highest-scoring hypothesis, if any.

        Check ``top.is_contested`` before acting on it. A winner that beat the
        runner-up by 0.2 points is not a conclusion.
        """
        return max(self.hypotheses, key=lambda h: h.score, default=None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = _plain(asdict(self))
        return out

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


def _plain(obj: Any) -> Any:
    """Make a structure JSON-safe without losing meaning."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if is_dataclass(obj) and not isinstance(obj, type):
        return _plain(asdict(obj))
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    return obj
