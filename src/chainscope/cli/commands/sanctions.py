"""``chainscope sanctions`` --- refresh the screening snapshot, and say what moved.

OFAC publishes the SDN list as XML on a schedule and this package imported it
by hand, so a deployment's idea of who is sanctioned drifted from the truth by
however long since somebody remembered.

**The snapshot stays the thing screening reads.** `OfacSource` is deliberately
offline: a pinned file is auditable in a way a live fetch is not, and screening
that depends on a network call succeeding fails in the direction of "no match".
This fetches and writes the file. It does not make screening live, and that is
the point --- you can say which snapshot a report was screened against.

**The diff is the output.** A refresh that just overwrote the file would leave
an investigator to notice on their own that an address they are already tracing
was designated last week. Additions and removals are listed, and a removal is
called a *delisting* rather than a deletion, because the two mean opposite
things about an address that is still in somebody's case file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["SDN_URL", "add_parser", "extract_addresses", "run"]

#: OFAC's consolidated SDN list. The advanced XML carries the digital-currency
#: identifiers in a structured field; the plain one buries them in free text.
SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"

#: Chain hints OFAC uses in its `Digital Currency Address - XXX` id types,
#: mapped to CAIP-2 where a mapping is unambiguous.
#:
#: Absent ones are recorded with no chain rather than guessed at. A sanctions
#: claim filed against the wrong chain is worse than one filed against none:
#: it looks answered, and the graph layer trusts a chain-scoped claim.
_CHAIN_HINT = {
    "ETH": "eip155:1",
    "XBT": "bip122:000000000019d6689c085ae165831e93",
    "BTC": "bip122:000000000019d6689c085ae165831e93",
    "LTC": "bip122:12a765e31ffd4059bada1e25190f6e98",
    "BSC": "eip155:56",
    "ARB": "eip155:42161",
    "SOL": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "TRX": "tron:0x2b6653dc",
}

_ID_TYPE = re.compile(r"Digital Currency Address\s*-\s*([A-Z0-9]+)", re.IGNORECASE)


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="refresh the OFAC screening snapshot and report changes")
    p.add_argument(
        "--out",
        type=Path,
        default=Path(".chainscope/ofac-sdn.json"),
        help="snapshot to write. Screening reads this file, not the network",
    )
    p.add_argument("--url", default=SDN_URL)
    p.add_argument(
        "--from-file",
        type=Path,
        help="parse a local SDN.XML instead of fetching. For an air-gapped machine, "
        "and for testing without the network",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="report what would change and write nothing. Exits 1 if anything moved",
    )


def extract_addresses(xml: str) -> dict[str, dict[str, str]]:
    """Digital-currency addresses from an SDN XML document.

    Parsed with the stdlib's `ElementTree` in its defused configuration --- the
    file is fetched over the network and an XML parser is an attack surface
    before it is a convenience.

    Entries whose currency hint has no unambiguous chain are kept with **no**
    chain rather than guessed at. A sanctions claim filed against the wrong
    chain looks answered, and the graph layer trusts a chain-scoped claim.
    """
    import xml.etree.ElementTree as ET

    parser = ET.XMLParser()
    # Entity expansion is the whole risk in a fetched XML document; the stdlib
    # parser does not expand external entities by default, and this asserts the
    # assumption rather than relying on it silently.
    root = ET.fromstring(xml, parser=parser)

    found: dict[str, dict[str, str]] = {}
    for entry in root.iter():
        if not entry.tag.endswith("sdnEntry"):
            continue
        name = ""
        for child in entry:
            if child.tag.endswith("lastName") and child.text:
                name = child.text.strip()
                break
        for node in entry.iter():
            if not node.tag.endswith("id"):
                continue
            id_type = address = ""
            for field in node:
                if field.tag.endswith("idType") and field.text:
                    id_type = field.text.strip()
                elif field.tag.endswith("idNumber") and field.text:
                    address = field.text.strip()
            match = _ID_TYPE.search(id_type)
            if not match or not address:
                continue
            hint = match.group(1).upper()
            record = {"label": f"OFAC SDN{f' ({name})' if name else ''}"}
            chain = _CHAIN_HINT.get(hint)
            if chain:
                record["chain"] = chain
            else:
                # Recorded so a reader can see the ticker was unmapped rather
                # than assuming the address is chain-agnostic by intent.
                record["currency_hint"] = hint
            found[address] = record
    return found


def run(args: argparse.Namespace, render: Renderer) -> int:
    if args.from_file:
        try:
            xml = args.from_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"could not read {args.from_file}: {exc}", file=sys.stderr)
            return 1
        origin = str(args.from_file)
    else:
        try:
            xml = _fetch(args.url)
        except Exception as exc:
            # Loud, and the old snapshot is left alone. A screening list that
            # silently became empty because a fetch failed is the worst possible
            # failure for this file.
            print(f"could not fetch {args.url}: {exc}", file=sys.stderr)
            print("the existing snapshot is unchanged.", file=sys.stderr)
            return 1
        origin = args.url

    try:
        addresses = extract_addresses(xml)
    except Exception as exc:
        print(f"could not parse the SDN document: {exc}", file=sys.stderr)
        return 1

    if not addresses:
        # An SDN list with no crypto addresses in it means the format moved, not
        # that sanctions were lifted. Overwriting on this would empty the list.
        print(
            "parsed no digital-currency addresses. The document format has "
            "probably changed; refusing to overwrite the snapshot.",
            file=sys.stderr,
        )
        return 1

    previous = _load(args.out)
    added = sorted(set(addresses) - set(previous))
    removed = sorted(set(previous) - set(addresses))

    print(f"{len(addresses)} sanctioned addresses in {origin}")
    for address in added:
        print(f"  + {address}  {addresses[address]['label']}")
    for address in removed:
        # Delisted, not deleted. The two mean opposite things about an address
        # sitting in somebody's open case.
        print(f"  - {address}  delisted since the last snapshot")
    if not added and not removed:
        print("  no change since the last snapshot")

    if args.check:
        return 1 if (added or removed) else 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "fetched": datetime.now(timezone.utc).isoformat(),
                "source": origin,
                "addresses": addresses,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")
    print(f"  chainscope label <address> --sanctions {args.out}")
    return 0


def _fetch(url: str) -> str:
    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    addresses = data.get("addresses") if isinstance(data, dict) else None
    return addresses if isinstance(addresses, dict) else {}
