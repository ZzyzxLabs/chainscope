"""Community-reported scam addresses, from a list anybody can actually obtain.

Surveying the OSINT sources a well-known on-chain investigation toolkit
recommends, and then *checking each one*, produced a short answer: most of them
cannot be reached. Measured:

===========================  =======================================
CryptoScamDB API             502, the service is down
Chainabuse API               401, needs a key
Etherscan label export       403 to anything that is not a browser
Blockscout ``public_tags``   present in the schema, empty in practice
===========================  =======================================

`MyEtherWallet/ethereum-lists` is the one that answers: 715 reported addresses,
no key, MIT-licensed, and each entry carries a free-text comment and a date.
That combination is rarer than the length of the recommendation lists suggests.

**What this source is worth, stated plainly.**

It is a *community* list. Entries are submitted by people who were defrauded or
who investigated a fraud, reviewed by maintainers, and merged. That is real
evidence and it is not a published legal fact — which is why this asserts
``MEDIUM`` and :mod:`chainscope.attribution.sources.ofac` asserts ``CERTAIN``.
The distinction is the whole reason both exist.

Its comments are the useful part and are preserved verbatim as the rationale,
because "XRP phishing website (ripple.com.pt) this wallet collects funds from
multiple victims" tells an investigator what happened, and ``scam`` does not.

**It goes stale in one direction.** An address reported in 2018 was reported in
2018; the list does not un-report. So a hit is a statement about the past, and
the entry's date travels with it so nobody reads a seven-year-old report as a
current one.

**Absence means nothing at all.** 715 addresses is a rounding error against the
number of scam addresses in existence, and the list skews heavily towards the
phishing campaigns of one era. A clean result here is not a clean result, which
is why :meth:`lookup` returns nothing rather than anything resembling an
all-clear, and why the resolver's distinction between "nothing known" and "could
not check" matters more for this source than for most.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ETHEREUM, ChainId
from ..base import Source, SourceError, SourceMeta

__all__ = ["DEFAULT_URL", "DarklistSource"]

#: Where the list lives. Raw GitHub, so no API token and no rate limit worth
#: worrying about --- and a URL a reader can open to check any claim made from it.
DEFAULT_URL = (
    "https://raw.githubusercontent.com/MyEtherWallet/ethereum-lists/"
    "master/src/addresses/addresses-darklist.json"
)


def _key(address: str) -> str:
    """Fold an EVM address, leave anything else exactly as written.

    This list is Ethereum-only in practice, but the guard is the same one
    :mod:`ofac` carries and for the same reason: lowercasing a base58 address
    both invents a match against an address nobody listed and loses the one that
    was. On a list whose purpose is accusing an address, either direction is
    unacceptable.
    """
    text = address.strip()
    if text.startswith(("0x", "0X")) and len(text) == 42:
        return text.lower()
    return text


class DarklistSource(Source):
    """Reported scam addresses from a local copy of ethereum-lists' darklist.

    Expected file shape --- the upstream JSON, unmodified::

        [
          {"address": "0x0975…", "comment": "XRP phishing website (…)",
           "date": "2018-01-16T00:00:00.000Z"}
        ]

    Fetched rather than bundled. The list changes, a bundled copy would silently
    become the version this package shipped with, and the ``source`` string
    records which snapshot answered so a hit can be checked against the
    publisher later.
    """

    name = "darklist"

    def __init__(self, path: Path | str = "data/labels/darklist.json") -> None:
        self.path = Path(path)
        self.meta = SourceMeta(
            publisher="MyEtherWallet/ethereum-lists contributors",
            license="MIT",
            redistributable=True,
            url="https://github.com/MyEtherWallet/ethereum-lists",
        )
        self._entries: dict[str, dict[str, Any]] | None = None

    def ready(self) -> bool:
        """Whether the data is present.

        Separate from `lookup` returning nothing, and the separation is the
        point: a source that answers "no reports" because its file is missing
        looks exactly like a clean screening result. The resolver asks this so
        it can mark an answer unreliable rather than reporting a hole as a pass.
        """
        return self.path.is_file()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        if not self.ready():
            raise SourceError(
                f"no darklist at {self.path}. Fetch it with "
                f"`chainscope sanctions --refresh-darklist`, or download "
                f"{DEFAULT_URL} to that path. Until then this source reports "
                f"nothing, and nothing is not the same as clean"
            )
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"darklist at {self.path} could not be read: {exc}") from exc
        if not isinstance(raw, list):
            raise SourceError(
                f"darklist at {self.path} is not a JSON array. The upstream file "
                f"is a list of objects; something else was downloaded"
            )

        entries: dict[str, dict[str, Any]] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            address = str(row.get("address") or "").strip()
            if not address:
                continue
            # First entry wins. The list contains the same address more than
            # once with different comments, and merging them would produce a
            # rationale nobody wrote.
            entries.setdefault(_key(address), row)
        self._entries = entries
        return entries

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """Reports against this address. Empty means *not on this list*.

        Not "clean". 715 addresses is a rounding error against the number that
        exist, and the list skews towards one era's phishing campaigns.
        """
        # Ethereum-only. Returning nothing for a Bitcoin address is correct and
        # returning it *silently* is not --- an unqualified empty result from a
        # source that was never going to answer reads as a screening pass.
        if chain is not None and chain != ETHEREUM:
            raise SourceError(
                f"the darklist covers Ethereum only; it has nothing to say about "
                f"{chain}, which is different from saying the address is clean"
            )

        row = self._load().get(_key(address))
        if row is None:
            return []

        comment = str(row.get("comment") or "").strip()
        when = _parse_date(row.get("date"))
        return [
            Attribution(
                address=address,
                chain=ETHEREUM,
                label=_label(comment),
                category=Category.SCAM,
                # MEDIUM, and deliberately not higher. A community report is
                # somebody's account of being defrauded --- real evidence, and
                # not the published legal fact that lets `ofac` say CERTAIN.
                confidence=Confidence.MEDIUM,
                method=Method.LIST,
                source=f"ethereum-lists darklist ({self.path.name})",
                # Verbatim. "XRP phishing website (ripple.com.pt) this wallet
                # collects funds from multiple victims" tells an investigator
                # what happened; "scam" does not.
                rationale=(
                    f"{comment} [reported {when.date().isoformat()}]"
                    if comment and when
                    else comment or "listed with no comment"
                ),
                observed_at=when,
            )
        ]


def _label(comment: str) -> str:
    """A short name from a free-text report.

    The first clause, capped. The full text goes in the rationale --- a label is
    read in a table where a sentence would be truncated by the renderer anyway,
    and truncation chosen here is better than truncation chosen by a column
    width.
    """
    if not comment:
        return "reported address"
    head = comment.split(".")[0].split(" this ")[0].strip()
    return head[:60] if head else "reported address"


def _parse_date(value: Any) -> datetime | None:
    """The report date, or ``None`` if it is unusable.

    ``None`` rather than "now". A missing date read as today would turn a
    2018 report into a current one, which is the single most misleading thing
    this file could do with an absent field.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
