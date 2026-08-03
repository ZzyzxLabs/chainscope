"""Append-only record of every outbound query.

An investigation whose queries were not recorded cannot be defended. When
someone asks "how do you know that", the answer needs to be a list of requests
and their responses, not a recollection.

The log is deliberately boring: one JSON object per line, append-only, no
rotation, no compaction. It is meant to be greppable a year later by someone who
has never seen this codebase.

Credentials never reach it --- see :mod:`chainscope.transport.credentials`, which
owns that definition so the audit log, the cache key, and the cassette recorder
cannot disagree about what a secret is. A log that leaks the API key it recorded
is worse than no log.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .credentials import redact, redact_headers, scrub_params

__all__ = ["AuditLog", "AuditRecord", "redact", "redact_headers"]


@dataclass(frozen=True, slots=True)
class AuditRecord:
    timestamp: datetime
    kind: str
    """``http.get``, ``rpc.eth_getLogs``, ``cache.hit`` --- whatever identifies the
    operation to a reader who was not there."""

    url: str
    provider: str | None = None
    status: int | None = None
    cache_key: str | None = None
    cached: bool = False
    duration_ms: float | None = None
    error: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "kind": self.kind,
            "url": redact(self.url),
            "provider": self.provider,
            "status": self.status,
            "cache_key": self.cache_key,
            "cached": self.cached,
            "ms": round(self.duration_ms, 1) if self.duration_ms else None,
            "error": self.error,
            "params": _redact_params(self.params),
        }


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    scrubbed = scrub_params(params)
    return scrubbed if isinstance(scrubbed, dict) else {}


class AuditLog:
    """Thread-safe JSON Lines writer."""

    def __init__(self, path: Path | str | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled and self.path is not None
        self._lock = threading.Lock()
        self._buffer: list[AuditRecord] = []

    def record(self, rec: AuditRecord) -> None:
        with self._lock:
            self._buffer.append(rec)
            if not self.enabled or self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec.to_dict(), default=str) + "\n")

    def log(self, kind: str, url: str, **kw: Any) -> None:
        self.record(AuditRecord(timestamp=datetime.now(timezone.utc), kind=kind, url=url, **kw))

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        """Records from this session. The file holds every session."""
        with self._lock:
            return tuple(self._buffer)

    def mark(self) -> int:
        """A position in the log, to bound a later `query_keys` to one window.

        Opaque on purpose: it is the buffer length, but nothing outside should
        rely on that.
        """
        with self._lock:
            return len(self._buffer)

    def query_keys(self, since: int = 0) -> tuple[str, ...]:
        """Cache keys touched since ``since`` --- the raw material for ``Evidence``.

        ``since`` defaults to the whole session, which is what an attestation of
        a run wants. An individual ``Result`` wants the window its own analyzer
        ran in: unbounded, its evidence lists every request the process made,
        including those belonging to other analyzers and other addresses. That
        reads as provenance and is not --- it cannot show which response
        produced which finding, so an attestation over it hashes the right
        bytes while proving nothing about the link between them.
        """
        with self._lock:
            window = self._buffer[since:]
            return tuple(dict.fromkeys(r.cache_key for r in window if r.cache_key))

    def replay(self) -> Iterator[dict[str, Any]]:
        """Read back a log file, including sessions other than this one."""
        if self.path is None or not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            recs = list(self._buffer)
        hits = sum(1 for r in recs if r.cached)
        errs = sum(1 for r in recs if r.error)
        by_provider: dict[str, int] = {}
        for r in recs:
            if r.provider:
                by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
        return {
            "queries": len(recs),
            "cache_hits": hits,
            "cache_hit_rate": round(hits / len(recs), 3) if recs else 0.0,
            "errors": errs,
            "by_provider": by_provider,
        }
