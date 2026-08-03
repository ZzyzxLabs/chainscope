"""``chainscope labels`` --- see what naming data is present, and fetch more.

The label datasets are the difference between a graph of twenty hex strings and
a graph of named entities, and until now getting them meant running
``python -c "from chainscope.attribution.sources... import clone; clone()"``.
A capability reachable only that way is one nobody reaches.

**Nothing is fetched implicitly.** Each dataset is downloaded when asked for and
never during a lookup: a resolve that quietly reached the network would make an
offline run behave differently from an online one with nothing said, and this
package's argument is that a result states where it came from.

**Nothing is bundled.** Two of these declare no licence permitting
redistribution and one is a repackaging of a third party's data, so they are
fetched into the case directory and gitignored. Shipping them would be a
licence violation dressed as convenience --- ``status`` prints each one's terms
so that is visible rather than buried in a docstring.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="show or fetch the address-label datasets")
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "fetch"],
        help="status: what is present and what each is worth. fetch: download",
    )
    p.add_argument(
        "which",
        nargs="?",
        help="darklist, eth-labels or contracts. Omit to fetch all three",
    )
    p.add_argument("--dir", type=Path, default=Path("data/labels"))


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...attribution.build import available_sources

    if args.action == "fetch":
        return _fetch(args)

    present = {s.name for s in available_sources(args.dir)}
    print(f"label datasets under {args.dir}\n")
    for name, note in _CATALOGUE.items():
        mark = "present" if name in present else "absent "
        print(f"  [{mark}] {name:16} {note['ceiling']:8} {note['what']}")
        print(f"                             {note['terms']}")
    if not present:
        # Said, because the consequence is silent: every address comes back
        # unlabelled and that reads as "nobody has named these" rather than
        # "nothing was consulted".
        print("\n  Nothing is present, so every lookup will report no attribution.")
        print("  That is not the same as an address being unknown. Run")
        print("  `chainscope labels fetch` to change it.")
        return 1
    return 0


#: What each dataset is and what its terms allow, printed rather than buried.
_CATALOGUE: dict[str, dict[str, str]] = {
    "ofac": {
        "ceiling": "CERTAIN",
        "what": "sanctions designations --- the only published legal fact here",
        "terms": "US government work. Fetch from the publisher; see docs/data-sources.md",
    },
    "local": {
        "ceiling": "MEDIUM",
        "what": "your own labels",
        "terms": "yours",
    },
    "explorer_nametags": {
        "ceiling": "HIGH",
        "what": "an explorer export you obtained yourself",
        "terms": "upstream terms apply --- not redistributable",
    },
    "contracts_list": {
        "ceiling": "MEDIUM",
        "what": "252k named contracts, each recording its own source",
        "terms": "NO LICENCE DECLARED --- fetched, never redistributed",
    },
    "eth_labels": {
        "ceiling": "MEDIUM",
        "what": "17k addresses: exchanges, hacks, token contracts",
        "terms": "MIT repo over Etherscan's data --- not redistributable",
    },
    "darklist": {
        "ceiling": "MEDIUM",
        "what": "715 community scam reports, each with a comment and a date",
        "terms": "MIT",
    },
}


def _fetch(args: argparse.Namespace) -> int:
    from ...attribution.sources.contracts_list import clone as clone_contracts
    from ...attribution.sources.darklist import DEFAULT_URL as DARKLIST_URL
    from ...attribution.sources.ethlabels import fetch as fetch_labels

    wanted = {args.which} if args.which else {"darklist", "eth-labels", "contracts"}
    args.dir.mkdir(parents=True, exist_ok=True)
    failed = False

    if "darklist" in wanted:
        failed |= _run("darklist", lambda: _download(DARKLIST_URL, args.dir / "darklist.json"))
    if "eth-labels" in wanted:
        failed |= _run(
            "eth-labels", lambda: sum(fetch_labels(args.dir / "eth-labels").values())
        )
    if "contracts" in wanted:
        # Named as a clone rather than a download, because 45 MB and a git
        # dependency are things somebody should know before it starts.
        print("  contracts: shallow git clone, ~45 MB")
        failed |= _run("contracts", lambda: clone_contracts(args.dir / "contracts"))

    if failed:
        print("\n  Some datasets did not arrive. The ones that did are usable ---")
        print("  a partial set is not a broken one, but a lookup over it is a")
        print("  lookup over less than you asked for.")
        return 1
    return 0


def _run(name: str, work: Any) -> bool:
    """Fetch one dataset. Returns whether it failed.

    Failures do not stop the others: a network blip on one list should not cost
    somebody the two that already downloaded.
    """
    try:
        count = work()
    except Exception as exc:
        print(f"  {name:12} FAILED  {type(exc).__name__}: {exc}")
        return True
    print(f"  {name:12} {count:,} entries")
    return False


def _download(url: str, into: Path) -> int:
    import json
    import urllib.request

    request = urllib.request.Request(url, headers={"user-agent": "chainscope"})
    with urllib.request.urlopen(request, timeout=60) as reply:
        rows = json.loads(reply.read())
    into.write_text(json.dumps(rows))
    return len(rows)
