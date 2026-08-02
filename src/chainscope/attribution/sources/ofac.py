"""OFAC sanctions screening.

The only source in this package permitted to assert ``CERTAIN``: a government
sanctions designation is a published legal fact, not an inference.

Two properties matter more here than in any other source.

**It must not fail quietly.** A sanctions source that returns an empty result
because its data file is missing looks exactly like a clean screening result.
:meth:`ready` and :class:`SourceError` exist so the resolver can tell the
difference and mark the answer unreliable.

**Mirrors are not authoritative.** Machine-readable extracts of the SDN list are
convenient and are what this adapter consumes, but the official list is the one
that carries legal weight. Before a sanctions hit affects a real decision,
verify against the publisher. The ``source`` string records which snapshot was
used precisely so that check is possible later.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ChainId
from ..base import Source, SourceError, SourceMeta

__all__ = ["OfacSource"]


class OfacSource(Source):
    """Sanctioned digital-currency addresses from a local SDN extract.

    Expected file shape::

        {
          "fetched": "2026-08-01T00:00:00Z",
          "source": "OFAC SDN list",
          "addresses": {
            "0xabc…": {"label": "OFAC SDN (ETH)", "chain": "eip155:1"}
          }
        }

    Deliberately offline. Screening should not depend on a network call
    succeeding, and a locally pinned snapshot is auditable in a way a live fetch
    is not.
    """

    name = "ofac-sdn"
    offline = True

    def __init__(
        self,
        path: Path | str,
        *,
        publisher: str = "US Treasury OFAC (via local extract)",
    ) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] | None = None
        self._fetched: datetime | None = None
        self.meta = SourceMeta(
            publisher=publisher,
            license="public domain (US government work)",
            redistributable=True,
            url="https://sanctionslist.ofac.treas.gov/Home/SdnList",
            snapshot=self._read_snapshot(),
            max_confidence=Confidence.CERTAIN,
        )

    def _read_snapshot(self) -> datetime | None:
        if not self.path.exists():
            return None
        # A missing or malformed 'fetched' field falls back to file mtime --- a
        # worse provenance stamp, but better than none.
        with contextlib.suppress(json.JSONDecodeError, ValueError, OSError, KeyError):
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if stamp := raw.get("fetched"):
                return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)

    # ---------------------------------------------------------------- loading

    def ready(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            raise SourceError(
                f"sanctions list not found at {self.path}. Screening cannot be "
                f"performed --- do not read an empty result as 'not sanctioned'."
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.path} is not valid JSON: {exc}") from exc
        entries = raw.get("addresses", raw)
        if not isinstance(entries, dict):
            raise SourceError(f"{self.path}: expected an 'addresses' object")
        self._data = {
            k.lower(): (v if isinstance(v, dict) else {"label": str(v)})
            for k, v in entries.items()
        }
        return self._data

    @property
    def count(self) -> int:
        try:
            return len(self._load())
        except SourceError:
            return 0

    # ---------------------------------------------------------------- queries

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        rec = self._load().get(address.lower())
        return [self._build(address, rec, chain)] if rec else []

    def lookup_many(
        self, addresses: Iterable[str], chain: ChainId | None = None
    ) -> dict[str, list[Attribution]]:
        data = self._load()
        out: dict[str, list[Attribution]] = {}
        for a in addresses:
            rec = data.get(a.lower())
            out[a] = [self._build(a, rec, chain)] if rec else []
        return out

    def _build(self, address: str, rec: dict[str, Any], chain: ChainId | None) -> Attribution:
        chain_id = chain
        if rec.get("chain"):
            # A malformed chain field in someone's label file should not abort
            # the lookup; fall back to the caller's chain.
            with contextlib.suppress(ValueError):
                chain_id = ChainId.parse(str(rec["chain"]))
        return self.emit(
            address=address,
            chain=chain_id,
            label=str(rec.get("label", "OFAC SDN")),
            category=Category.SANCTIONED,
            confidence=Confidence.CERTAIN,
            method=Method.LIST,
            rationale=(
                "listed on the OFAC Specially Designated Nationals list; "
                "verify against the official publication before acting"
            ),
            tags=frozenset({"sanctions", "ofac"}),
        )
