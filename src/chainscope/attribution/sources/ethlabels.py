"""Named addresses, from the largest open dump that exists.

The gap this closes is the one visible on the first screen of the web page: a
graph of twenty addresses, every one of them reading `unlabelled`. Structure
tells you the money went somewhere; it never tells you where, and a case that
cannot name a single counterparty is a case nobody can act on.

Surveyed against what is actually reachable in August 2026 --- Dune's label
tables need a key, WalletLabels is a commercial API, Blockscout's public tags
have no cross-instance dump, and OpenChain turned out to be function selectors
rather than addresses at all --- `dawsbot/eth-labels` is the largest open
option: MIT, no key, and roughly 170,000 entries across four categories.

**Its provenance is weak and that decides the ceiling.** The dump is
Etherscan's label data, reorganised. Etherscan does not publish it under a
licence that permits this, and the repository is a copy rather than an
independently gathered dataset --- so an entry here is *somebody's transcription
of somebody else's judgement*, with no evidence attached and no way to ask who
decided or when.

That is worth having and it is not worth `HIGH`. This asserts ``MEDIUM``, one
step below :mod:`chainscope.attribution.sources.etherscan_dump`'s ceiling for
data the user obtained themselves, and two below the ``CERTAIN`` a published
sanctions designation earns.

**The category matters more than the name.** `phish-hack` holds 5,594 addresses
named things like "Bancor Hacker" --- and a name like that in a report is an
accusation. It is carried as :attr:`Category.ILLICIT` with the upstream
category in the rationale, so a reader can see the claim rests on a community
list rather than on anything this tool verified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ETHEREUM, ChainId
from ..base import Source, SourceError, SourceMeta

__all__ = ["CATEGORIES", "DEFAULT_BASE", "EthLabelsSource"]

DEFAULT_BASE = "https://raw.githubusercontent.com/dawsbot/eth-labels/master/src/mainnet"

#: Upstream category to ours, with the ones that are *not* a straight mapping
#: called out.
#:
#: `phish-hack` becomes ILLICIT rather than SCAM because the upstream bucket
#: mixes both --- "Bancor Hacker" is a theft and "Fake_Phishing" is a fraud, and
#: collapsing them into the narrower word would state something the data does
#: not support.
#:
#: `genesis` is not illicit or anything else; it is a fact about an address's
#: origin, so it maps to the neutral bucket rather than being dropped.
CATEGORIES: dict[str, Category] = {
    "exchange": Category.CEX,
    "phish-hack": Category.ILLICIT,
    "token-contract": Category.TOKEN,
    "genesis": Category.CONTRACT,
}


class EthLabelsSource(Source):
    """Address labels from a local copy of the eth-labels dump.

    Expected layout, mirroring upstream::

        <path>/exchange.json
        <path>/phish-hack.json
        <path>/token-contract.json
        <path>/genesis.json

    each holding ``[{"address": "0x…", "nameTag": "Bancor Hacker"}, …]``.

    Fetched rather than bundled, like every other dataset here: a committed copy
    silently becomes the version this package shipped with, and for a file whose
    entries accuse addresses a stale snapshot is worse than no snapshot.
    """

    name = "eth_labels"

    def __init__(self, path: Path | str = "data/labels/eth-labels") -> None:
        self.path = Path(path)
        self.meta = SourceMeta(
            publisher="dawsbot/eth-labels (reorganised from Etherscan)",
            license="MIT (the repository; the underlying labels are Etherscan's)",
            # Not redistributable, and the distinction is the point: the
            # repository's licence covers its own arrangement of the data, not
            # the data. Marking it otherwise would invite shipping it.
            redistributable=False,
            url="https://github.com/dawsbot/eth-labels",
        )
        self._entries: dict[str, tuple[str, str]] | None = None

    def ready(self) -> bool:
        """Whether any category file is present.

        Any, not all: a user who fetched only `phish-hack` has a usable source,
        and refusing until every file exists would turn a partial download into
        a silent clean screening --- which is the failure `ready` exists to
        prevent.
        """
        return self.path.is_dir() and any(
            (self.path / f"{name}.json").is_file() for name in CATEGORIES
        )

    def _load(self) -> dict[str, tuple[str, str]]:
        if self._entries is not None:
            return self._entries
        if not self.ready():
            raise SourceError(
                f"no eth-labels data under {self.path}. Fetch the category files "
                f"from {DEFAULT_BASE}/<category>/all.json. Until then this source "
                f"reports nothing, and nothing is not the same as unlabelled"
            )

        entries: dict[str, tuple[str, str]] = {}
        for category in CATEGORIES:
            found = self.path / f"{category}.json"
            if not found.is_file():
                continue
            try:
                rows = json.loads(found.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise SourceError(f"{found} could not be read: {exc}") from exc
            if not isinstance(rows, list):
                raise SourceError(f"{found} is not a JSON array")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                address = str(row.get("address") or "").strip().lower()
                label = str(row.get("nameTag") or "").strip()
                # A row with no name is an address somebody put in a bucket and
                # never named. The bucket is still information --- "this is an
                # exchange" --- so it is kept with the category as the label
                # rather than dropped.
                if not address:
                    continue
                entries.setdefault(address, (label, category))
        self._entries = entries
        return entries

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """What this dump calls the address. Empty means it is not in it."""
        if chain is not None and chain != ETHEREUM:
            # Mainnet only. An unqualified empty result from a source that was
            # never going to answer reads as "nothing is known", which is a
            # different and much stronger statement.
            raise SourceError(
                f"eth-labels covers Ethereum mainnet only; it has nothing to say "
                f"about {chain}, which is not the same as the address being "
                f"unknown"
            )

        found = self._load().get(address.strip().lower())
        if found is None:
            return []
        label, category = found
        return [
            Attribution(
                address=address,
                chain=ETHEREUM,
                label=label or f"{category} (unnamed in the source)",
                category=CATEGORIES.get(category, Category.UNKNOWN),
                # MEDIUM. The dump is a transcription of Etherscan's judgement
                # with no evidence attached and no way to ask who decided or
                # when --- worth having, and not worth HIGH.
                confidence=Confidence.MEDIUM,
                method=Method.LIST,
                source=f"eth-labels {category} ({self.path.name})",
                rationale=(
                    f"listed under '{category}' in the eth-labels dump, which "
                    f"reorganises Etherscan's public tags. No evidence travels "
                    f"with the entry; treat the name as a lead into Etherscan "
                    f"rather than as a verified attribution"
                ),
            )
        ]


def fetch(path: Path | str = "data/labels/eth-labels", client: Any = None) -> dict[str, int]:
    """Download the category files. Returns how many rows each held.

    Separate from the source, and not called by it. A lookup that quietly
    reached the network would make an offline run behave differently from an
    online one with nothing said, and this package's whole argument is that a
    result states where it came from.
    """
    import urllib.request

    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for category in CATEGORIES:
        url = f"{DEFAULT_BASE}/{category}/all.json"
        request = urllib.request.Request(url, headers={"user-agent": "chainscope"})
        with urllib.request.urlopen(request, timeout=60) as reply:
            rows = json.loads(reply.read())
        (target / f"{category}.json").write_text(json.dumps(rows))
        counts[category] = len(rows)
    return counts
