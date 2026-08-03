"""TagPacks: the attribution format GraphSense publishes, read as a source.

Two projects arrived at the same shape independently. A TagPack tag carries a
label, a **mandatory** source, a confidence and a category --- which is this
package's `Attribution` with different field names. That convergence is why
reading them is a mapping rather than a translation, and it is the strongest
argument for using their format instead of inventing a third.

**What this buys.** `graphsense-tagpacks` publishes 523,988 attributed
addresses under MIT, across Bitcoin, Ethereum and others: exchange wallets,
mining pools, mixers, sextortion campaigns, and services identified by INTERPOL.
That is an order of magnitude more than every other source here combined, and
none of it had to be collected.

**Their confidence model is better than ours and this file says so.** Ours is an
abstract ladder (SPECULATIVE..CERTAIN) with a separate `Method`. Theirs is one
vocabulary keyed on *how the tag was obtained* --- `ownership` (the creator holds
the key) scores 100, `authority_data` (OFAC and the like) scores 60,
`web_crawl` 20, `heuristic` 10. Confidence and provenance cannot drift apart
because they are the same field.

The `authority_data` level is the interesting one. This package rates OFAC
`CERTAIN`; GraphSense rates it 60. They are right. A sanctions listing is an
authoritative *claim*, but the address-to-entity mapping inside it is still
somebody's research and has been wrong before. `_CONFIDENCE` below preserves
their judgement rather than flattening it into ours.

Format: https://github.com/graphsense/graphsense-tagpacks
Taxonomy: https://github.com/graphsense/DW-VA-Taxonomy
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ChainId
from ..base import Source, SourceError, SourceMeta

__all__ = ["DEFAULT_DIR", "REPO", "TagPackSource"]

REPO = "https://github.com/graphsense/graphsense-tagpacks"
DEFAULT_DIR = "data/labels/tagpacks"

#: Their confidence ids, with the level each carries and what this package
#: makes of it. Their levels are 0-100; ours is a five-step ladder, so the
#: mapping loses resolution --- the original id travels in the rationale so
#: nothing is thrown away.
#:
#: Deliberately NOT collapsing `authority_data` to CERTAIN. See the module
#: docstring: a sanctions listing is an authoritative claim about an entity,
#: and the address-to-entity mapping inside it is still research.
_CONFIDENCE: dict[str, tuple[int, Confidence]] = {
    "override": (100, Confidence.HIGH),
    "ownership": (100, Confidence.CERTAIN),
    "ledger_immanent": (100, Confidence.CERTAIN),
    "manual_transaction": (90, Confidence.HIGH),
    "service_api": (70, Confidence.HIGH),
    "forensic_investigation": (70, Confidence.HIGH),
    "authority_data": (60, Confidence.HIGH),
    "trusted_provider": (50, Confidence.MEDIUM),
    "service_data": (50, Confidence.MEDIUM),
    "forensic": (50, Confidence.MEDIUM),
    "untrusted_transaction": (40, Confidence.MEDIUM),
    "web_crawl": (20, Confidence.LOW),
    "heuristic": (10, Confidence.SPECULATIVE),
}

#: Their concept taxonomy onto ours. Unmapped concepts become `UNKNOWN` rather
#: than being guessed at --- a wrong category is a claim nobody made, and their
#: vocabulary is larger than ours by design.
_CATEGORY: dict[str, Category] = {
    "exchange": Category.CEX,
    "decentralized_exchange": Category.DEX,
    "defi": Category.DEX,
    "mixing_service": Category.MIXER,
    "bridge": Category.BRIDGE,
    "miner": Category.MINER,
    "mining_pool": Category.MINER,
    "gambling": Category.SERVICE,
    "wallet_service": Category.SERVICE,
    "hosted_wallet": Category.SERVICE,
    "payment_processor": Category.SERVICE,
    "merchant_service": Category.SERVICE,
    "marketplace": Category.SERVICE,
    "atm": Category.SERVICE,
    "scam": Category.SCAM,
    "ponzi_scheme": Category.SCAM,
    "sextortion": Category.SCAM,
    "phishing": Category.SCAM,
    "ransomware": Category.ILLICIT,
    "darknet_market": Category.ILLICIT,
    "stolen_funds": Category.ILLICIT,
    "theft": Category.ILLICIT,
    "malware": Category.ILLICIT,
    "sanctions": Category.SANCTIONED,
    "terrorism_financing": Category.SANCTIONED,
}

#: Their `currency` codes onto CAIP-2. Only what the corpus actually contains;
#: an unknown code yields a chain-agnostic claim rather than a guessed chain,
#: because attaching a claim to the wrong chain is worse than attaching it to
#: none.
_CHAIN: dict[str, str] = {
    "BTC": "bip122:000000000019d6689c085ae165831e93",
    "ETH": "eip155:1",
    "BCH": "bip122:000000000000000000651ef99cb9fcbe",
    "LTC": "bip122:12a765e31ffd4059bada1e25190f6e98",
    "ZEC": "bip122:0000000000196a45a4f0a1b0e5a0d4b6",
    "TRX": "tron:mainnet",
}


class TagPackSource(Source):
    """Attribution tags from a local checkout of the public TagPacks.

    Header fields are inherited by every tag in the pack and overridden per
    tag, which is how the format keeps 50,000-address files readable. Both
    levels are honoured here; a tag that sets its own `confidence` wins over
    the pack's.
    """

    name = "tagpack"

    def __init__(self, path: Path | str = DEFAULT_DIR) -> None:
        self.path = Path(path)
        self.meta = SourceMeta(
            publisher="GraphSense / Iknaio Cryptoasset Analytics GmbH and contributors",
            license="MIT",
            redistributable=True,
            url=REPO,
        )
        self._index: dict[str, list[dict[str, Any]]] | None = None

    def ready(self) -> bool:
        """Whether a checkout is present.

        Separate from `lookup` returning nothing, and the separation is the
        point: a source that answers "no tags" because its directory is missing
        looks exactly like a clean screening result.
        """
        return self.path.is_dir() and any(self.path.rglob("*.yaml"))

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._index is not None:
            return self._index
        if not self.ready():
            raise SourceError(
                f"no tagpacks at {self.path}. Clone {REPO} there "
                f"(`chainscope labels fetch tagpack`). Until then this source "
                f"reports nothing, and nothing is not the same as clean"
            )
        try:
            import yaml
        except ImportError as exc:
            # Optional on purpose: the corpus is a separate half-million-address
            # download, and somebody who never fetches it should not carry a
            # YAML parser. Naming the extra matters --- "no module named yaml"
            # sends a reader to pip install yaml, which is a different package.
            raise SourceError(
                "reading tagpacks needs PyYAML, which is an optional extra "
                "here: `pip install 'chainscope[tagpacks]'`"
            ) from exc

        index: dict[str, list[dict[str, Any]]] = {}
        for file in sorted(self.path.rglob("*.yaml")):
            try:
                pack = yaml.safe_load(file.read_text(errors="replace"))
            except Exception:
                # One malformed pack must not cost the other seventy-six.
                continue
            if not isinstance(pack, dict):
                continue
            header = {k: v for k, v in pack.items() if k != "tags"}
            header["_pack"] = file.name
            for tag in pack.get("tags") or ():
                if not isinstance(tag, dict):
                    continue
                address = str(tag.get("address") or "").strip()
                if not address:
                    continue
                merged = {**header, **tag}
                index.setdefault(_fold(address), []).append(merged)
        self._index = index
        return index

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """Tags for this address. Empty means *not in this corpus*.

        Not "clean". 523,988 addresses is large but it is a curated collection,
        heavily weighted towards Bitcoin services and a few campaigns.
        """
        rows = self._load().get(_fold(address), [])
        out: list[Attribution] = []
        for row in rows:
            claim = _to_attribution(address, row)
            if claim is None:
                continue
            if chain is not None and claim.chain is not None and claim.chain != chain:
                continue
            out.append(claim)
        return out


def _fold(address: str) -> str:
    """Fold an EVM address, leave anything else exactly as written.

    The corpus is mostly base58 Bitcoin addresses, where lowercasing both
    invents a match against an address nobody listed and loses the one that
    was. Same rule as every other source here.
    """
    text = address.strip()
    if text.startswith(("0x", "0X")) and len(text) == 42:
        return text.lower()
    return text


def _to_attribution(address: str, row: dict[str, Any]) -> Attribution | None:
    label = str(row.get("label") or "").strip()
    if not label:
        return None

    level, confidence = _CONFIDENCE.get(
        str(row.get("confidence") or "").strip(), (0, Confidence.SPECULATIVE)
    )
    category = _CATEGORY.get(str(row.get("category") or "").strip().lower(), Category.UNKNOWN)
    chain = _chain_of(row)

    # Their id and numeric level travel in the rationale. Our five-step ladder
    # is coarser than their hundred-point scale, so the mapping loses
    # resolution --- keeping the original means a reader can recover it, and a
    # future version can use it directly.
    parts = [f"tagpack {row.get('_pack')}"]
    if row.get("confidence"):
        parts.append(f"confidence={row['confidence']} ({level}/100)")
    if row.get("actor"):
        parts.append(f"actor={row['actor']}")
    if row.get("context"):
        parts.append(str(row["context"])[:200])

    return Attribution(
        address=address,
        chain=chain,
        label=label,
        category=category,
        confidence=confidence,
        method=Method.LIST,
        source=f"{row.get('title') or 'TagPack'} via {row.get('creator') or 'unknown creator'}",
        rationale="; ".join(parts),
        observed_at=_when(row.get("lastmod")),
    )


def _chain_of(row: dict[str, Any]) -> ChainId | None:
    """The CAIP-2 chain, or None when the code is unrecognised.

    None means "applies everywhere", which is how a chain-agnostic claim is
    represented here --- and it is the safe answer, because attaching a tag to
    the wrong chain asserts something about twenty bytes on a network the
    tagger never looked at.
    """
    code = str(row.get("network") or row.get("currency") or "").strip().upper()
    caip = _CHAIN.get(code)
    if caip is None:
        return None
    try:
        return ChainId.parse(caip)
    except Exception:
        return None


def _when(value: Any) -> datetime | None:
    """The tag's date, or None. Never "now" --- see `darklist._parse_date`."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
