"""Labels you maintain yourself.

Every investigation produces attribution that no public dataset has: the deposit
address cluster you identified last week, the wallet a counterparty confirmed by
email. Public label coverage is uneven and always lags, so this is not a
fallback --- it is where most of the value accumulates over time.

Claims are capped at ``MEDIUM`` unless the file records where they came from.
An analyst's own note is evidence of an analyst's opinion; without a stated
basis it should not outrank a published label.
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

__all__ = ["LocalSource"]

_METHODS = {m.value: m for m in Method}
_CATEGORIES = {c.value: c for c in Category}
_CONFIDENCE = {c.name.lower(): c for c in Confidence}


class LocalSource(Source):
    """Attribution from a local JSON file.

    Expected shape --- a mapping of address to record::

        {
          "0xabc…": {
            "label": "Acme Exchange: hot wallet",
            "category": "cex",
            "confidence": "medium",
            "method": "heuristic",
            "rationale": "17 single-use deposit addresses consolidate here",
            "chain": "eip155:1",
            "tags": ["exchange"]
          }
        }

    Keys beginning with ``_`` are treated as file-level notes and skipped, so a
    ``_README`` entry can sit alongside the data.
    """

    offline = True

    def __init__(
        self,
        path: Path | str,
        *,
        name: str = "local",
        publisher: str = "analyst",
        license: str = "private",
        max_confidence: Confidence = Confidence.MEDIUM,
    ) -> None:
        self.path = Path(path)
        self.name = name
        snapshot = (
            datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
            if self.path.exists()
            else None
        )
        self.meta = SourceMeta(
            publisher=publisher,
            license=license,
            redistributable=False,
            url=str(self.path),
            snapshot=snapshot,
            max_confidence=max_confidence,
        )
        self._data: dict[str, dict[str, Any]] | None = None

    # ---------------------------------------------------------------- loading

    def ready(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            raise SourceError(f"{self.path} does not exist")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SourceError(f"{self.path} must contain a JSON object")
        self._data = {
            k.lower(): v
            for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        return self._data

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
        confidence = _CONFIDENCE.get(
            str(rec.get("confidence", "medium")).lower(), Confidence.MEDIUM
        )
        method = _METHODS.get(str(rec.get("method", "manual")).lower(), Method.MANUAL)
        rationale = str(rec.get("rationale", ""))
        if confidence <= Confidence.LOW and not rationale:
            # The constructor would reject this outright. Say why here so the
            # user can fix their file rather than read a traceback.
            rationale = (
                "no rationale recorded in the local label file; "
                "add a 'rationale' field explaining the basis for this claim"
            )
        chain_id = chain
        if rec.get("chain"):
            # A malformed chain field in someone's label file should not abort
            # the lookup; fall back to the caller's chain.
            with contextlib.suppress(ValueError):
                chain_id = ChainId.parse(str(rec["chain"]))
        return self.emit(
            address=address,
            chain=chain_id,
            label=str(rec.get("label", "unlabelled")),
            category=_CATEGORIES.get(
                str(rec.get("category", "unknown")).lower(), Category.UNKNOWN
            ),
            confidence=confidence,
            method=method,
            rationale=rationale,
            tags=frozenset(rec.get("tags", ())),
        )

    # ---------------------------------------------------------------- writing

    def add(
        self,
        address: str,
        label: str,
        *,
        category: Category = Category.UNKNOWN,
        confidence: Confidence = Confidence.MEDIUM,
        method: Method = Method.MANUAL,
        rationale: str = "",
        chain: ChainId | None = None,
        tags: Iterable[str] = (),
    ) -> None:
        """Record a finding so the next investigation starts from it."""
        if confidence <= Confidence.LOW and not rationale.strip():
            raise ValueError(
                "a LOW or SPECULATIVE claim needs a rationale --- what made you think this?"
            )
        data = self._load() if self.path.exists() else {}
        data[address.lower()] = {
            "label": label,
            "category": category.value,
            "confidence": confidence.name.lower(),
            "method": method.value,
            "rationale": rationale,
            "chain": str(chain) if chain else None,
            "tags": sorted(tags),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self._data = data
