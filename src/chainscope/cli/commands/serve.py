"""``chainscope serve`` --- open the case in a browser.

The web page was reachable only by writing Python::

    from chainscope.server import LocalServer, ServerOptions
    LocalServer(ServerOptions(store=..., port=8801, token="…")).start()

which is the same defect as the label datasets before `chainscope labels`
existed, in the larger of the two places: a picture you have to write a program
to look at is one nobody looks at.

**The token is generated, not chosen.** Asking for one at startup means a weak
one, or the same one twice, or one in somebody's shell history. It is minted
per run and injected into the page, which is served by the same server that
answers its requests --- so it never travels in a URL either.

**Read-only by default, and it says which.** The page can label addresses and
file notes, and both write to the case. That should be a decision somebody
made, not the default for a command they ran to look at something.

**Loopback only, and `--host` is deliberately absent.** The store holds
attributions somebody will act on and the server has no authentication beyond
the token. Binding wider is a thing to do on purpose, with a reverse proxy in
front, and not by passing a flag to a convenience command.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import webbrowser
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="open this case in a browser")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument("--port", type=int, default=8787)
    p.add_argument(
        "--writable",
        action="store_true",
        help="allow the page to record labels and notes. Off by default: "
        "writing to a case should be a decision, not the default for a command "
        "you ran to look at something",
    )
    p.add_argument(
        "--analyst",
        default="",
        help="who is recording. Empty means nobody is named, and the case says "
        "so rather than signing somebody's name to a claim",
    )
    p.add_argument("--no-open", action="store_true", help="do not launch a browser")
    p.add_argument(
        "--labels",
        type=Path,
        default=Path("data/labels"),
        help="directory of label datasets; everything present is consulted",
    )


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...attribution.build import available_sources

    # From the module, not the package:  resolves these through
    # __getattr__ for lazy import, which leaves them untyped at the call site.
    from ...server.local import LocalServer, ServerOptions

    if not args.store.exists():
        # Not fatal --- the page fetches an address it has never seen --- but
        # said, because an empty case and a missing one look identical once the
        # page is open.
        print(f"no store at {args.store} yet; it will be created as you fetch.\n")

    found = available_sources(args.labels)
    if not found:
        print(
            "no label datasets found, so every address will read as unlabelled.\n"
            "That is not the same as an address being unknown --- run\n"
            "`chainscope labels fetch` first if you want names.\n"
        )
    else:
        print(f"labels from: {', '.join(s.name for s in found)}\n")

    server = LocalServer(
        ServerOptions(
            store=args.store,
            port=args.port,
            # Minted per run. Asking for one gets a weak one, or the same one
            # twice, or one in a shell history.
            token=secrets.token_urlsafe(24),
            # The same directory the banner above reported on. It used to be
            # guessed from the store path inside the server, so the two
            # disagreed whenever --store pointed anywhere unusual.
            labels=args.labels,
            writable=args.writable,
            analyst=args.analyst,
        )
    ).start()

    print(f"  {server.url}")
    mode = (
        "writable --- the page can label and file notes"
        if args.writable
        else "read-only --- pass --writable to record labels and notes"
    )
    print(f"  {mode}")
    print("  loopback only. Ctrl-C to stop.\n")
    # The token is minted per run and written nowhere else, so this line is the
    # only copy of it. Python block-buffers stdout when it is not a terminal,
    # which is exactly the case for `chainscope serve > log &` or anything run
    # under a supervisor --- the server comes up, serves correctly, and the one
    # string needed to reach it sits in an 8KB buffer until the process exits.
    # Found by redirecting the output and getting an empty file.
    sys.stdout.flush()

    if not args.no_open:
        webbrowser.open(server.url)
    # `start` serves on a daemon thread, so this waits rather than serving. A
    # bare `pass` loop would spin; an Event that is never set parks the thread.
    import threading

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0
