"""Block-explorer nametags, from a community-maintained dump.

Explorer nametags are the best public attribution available for EVM chains, and
this adapter reads a bulk snapshot of them rather than calling an explorer:
nametags are not exposed by the free APIs, and scraping them is both rude and
fragile.

Two limitations users need to internalise, stated here because they change how
results should be read:

**Coverage is uneven and lags.** Large exchanges, DeFi protocols, and bridges
are well covered. Smaller and newer services are frequently absent entirely. An
unlabelled address is not an unknown entity --- it is an entity nobody has
published a label for yet, and that distinction matters when the absence is
about to be reported as a finding.

**Bitcoin has no equivalent.** There is no public nametag database for UTXO
chains, which is why attribution there falls back to clustering heuristics at
``MEDIUM`` and behavioural inference at ``LOW``.

Redistribution: the dump's license and the originating explorer's terms both
apply. chainscope reads such a file at run time and vendors nothing.
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

__all__ = ["ExplorerDumpSource"]

#: Tag substrings mapped onto the coarse categories traversal branches on.
#: Order matters --- the first match wins, and sanctions-adjacent tags are
#: checked before the generic ones.
_CATEGORY_HINTS: tuple[tuple[tuple[str, ...], Category], ...] = (
    (("tornado", "mixer", "coinjoin", "wasabi"), Category.MIXER),
    (("phish", "hack", "exploit", "heist", "drainer", "ransom"), Category.ILLICIT),
    (("scam", "fake", "spam", "honeypot"), Category.SCAM),
    (("bridge", "wormhole", "across", "stargate"), Category.BRIDGE),
    (
        (
            "exchange",
            "cex",
            "binance",
            "coinbase",
            "kraken",
            "okx",
            "kucoin",
            "bitfinex",
            "huobi",
            "htx",
            "gate.io",
            "bybit",
            "bitstamp",
            "gemini",
            "crypto.com",
            "hitbtc",
            "mexc",
        ),
        Category.CEX,
    ),
    (
        ("uniswap", "sushiswap", "curve", "balancer", "dex", "0x", "1inch", "pancakeswap"),
        Category.DEX,
    ),
    (("token", "erc20", "stablecoin"), Category.TOKEN),
    (("miner", "mining", "pool"), Category.MINER),
)


def _categorise(label: str, tags: list[str]) -> Category:
    haystack = " ".join([label, *tags]).lower()
    for needles, category in _CATEGORY_HINTS:
        if any(n in haystack for n in needles):
            return category
    return Category.SERVICE if label else Category.UNKNOWN


class ExplorerDumpSource(Source):
    """Bulk explorer nametags from a local JSON file.

    Expected shape --- address to record::

        {"0xabc…": {"label": "Binance 14", "chain": "eip155:1",
                    "tags": ["binance", "exchange"]}}
    """

    name = "explorer-nametags"
    offline = True

    def __init__(
        self,
        path: Path | str,
        *,
        publisher: str = "community dump of block-explorer nametags",
        license: str = "see upstream dataset and explorer terms",
        redistributable: bool = False,
    ) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] | None = None
        self.meta = SourceMeta(
            publisher=publisher,
            license=license,
            redistributable=redistributable,
            snapshot=(
                datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
                if self.path.exists()
                else None
            ),
            # HIGH, never CERTAIN. A third party published it; that is strong,
            # but it is not the chain speaking and not a legal designation.
            max_confidence=Confidence.HIGH,
        )

    def ready(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            raise SourceError(f"nametag dump not found at {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.path} is not valid JSON: {exc}") from exc
        self._data = {
            k.lower(): v
            for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        return self._data

    @property
    def count(self) -> int:
        try:
            return len(self._load())
        except SourceError:
            return 0

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
        label = str(rec.get("label") or rec.get("name") or "labelled")
        tags = [str(t) for t in rec.get("tags", ())]
        chain_id = chain
        if rec.get("chain"):
            # A malformed chain field in someone's label file should not abort
            # the lookup; fall back to the caller's chain.
            with contextlib.suppress(ValueError):
                chain_id = ChainId.parse(str(rec["chain"]))
        return self.emit(
            address=address,
            chain=chain_id,
            label=label,
            category=_categorise(label, tags),
            confidence=Confidence.HIGH,
            method=Method.LABEL,
            tags=frozenset(tags),
        )
