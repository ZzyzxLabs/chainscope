"""Append-only record of every outbound query.

An investigation whose queries were not recorded cannot be defended. When
someone asks "how do you know that", the answer needs to be a list of requests
and their responses, not a recollection.

The log is deliberately boring: one JSON object per line, append-only, no
rotation, no compaction. It is meant to be greppable a year later by someone who
has never seen this codebase.

Credentials never reach it --- see :func:`redact`. A log that leaks the API key
it recorded is worse than no log.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["AuditLog", "AuditRecord", "redact"]

# Query-string keys whose values are credentials. Matched case-insensitively.
_SECRET_PARAMS = (
    "apikey",
    "api_key",
    "key",
    "token",
    "access_token",
    "auth",
    "secret",
    "password",
    "signature",
)
_SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "tron-pro-api-key", "x-auth-token", "cookie"}
)

_PARAM_RE = re.compile(r"(?i)\b(" + "|".join(_SECRET_PARAMS) + r")=([^&\s]+)")
# Provider keys embedded in a path segment, e.g. /v2/<key>. Matches long
# opaque-looking segments only, so ordinary paths survive.
_PATH_KEY_RE = re.compile(r"/(v\d+)/([A-Za-z0-9_-]{20,})")


def redact(text: str) -> str:
    """Strip credentials from a URL or arbitrary string."""
    out = _PARAM_RE.sub(r"\1=<redacted>", text)
    return _PATH_KEY_RE.sub(r"/\1/<redacted>", out)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: ("<redacted>" if k.lower() in _SECRET_HEADERS else v) for k, v in headers.items()
    }


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
    return {k: ("<redacted>" if k.lower() in _SECRET_PARAMS else v) for k, v in params.items()}


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

    def query_keys(self) -> tuple[str, ...]:
        """Cache keys touched this session --- the raw material for ``Evidence``."""
        with self._lock:
            return tuple(dict.fromkeys(r.cache_key for r in self._buffer if r.cache_key))

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
