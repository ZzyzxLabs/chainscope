"""Package a case, hand it over, open somebody else's.

`docs/handover.md` has documented `bundle export` and `bundle import` for some
time; this file only inspected. That is the worst version of the gap, because a
reader following the handover guide got "invalid choice" from argparse and no
way to tell whether the workflow was missing or they had mistyped it.

What a case actually consists of is several files that nothing bound together:

=====================  ================================================
`.chainscope/case.db`  notes, leads, correspondence --- what a person wrote
query cache            the provider responses the findings rest on
audit log              what was asked, of whom, when
analyzer results       the findings themselves
report                 the prose written from them
=====================  ================================================

`export` collects them under one manifest; `import` unpacks it and says what it
found and what it did not. The split between the case record and the cache is
deliberate and is argued in `Bundle.attach_case`: the cache is derived and can
be rebuilt by re-running, the case record cannot be rebuilt by anything.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ...case.bundle import Bundle, BundleError
from ...render.base import Renderer

__all__ = ["add_parser", "run"]

DEFAULT_CASE = Path(".chainscope/case.db")
DEFAULT_CACHE = Path(".chainscope/cache.sqlite")
DEFAULT_AUDIT = Path(".chainscope/audit.jsonl")


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="package, inspect, and open case bundles")

    inner = p.add_subparsers(dest="bundle_cmd")

    # `show` is explicit *and* the default, because the README documents
    # `chainscope bundle theft.chainscope` with no verb. An optional positional
    # alongside subparsers cannot express that --- argparse binds the bare word
    # `export` to the positional and then rejects the real path as an invalid
    # choice --- so `main` inserts `show` when the first token is not a verb.
    s = inner.add_parser("show", help="what is inside a bundle, and whether it replays")
    s.add_argument("path", type=Path)
    s.add_argument("--archive", type=Path, help="zip the bundle to this path")

    e = inner.add_parser("export", help="package a case for handover")
    e.add_argument("dest", type=Path, help="directory to build the bundle in")
    e.add_argument("--title", default="", help="what this case is")
    e.add_argument("--subject", default="", help="the address or entity under study")
    e.add_argument("--notes", default="", help="anything the reader needs up front")
    e.add_argument("--case", type=Path, default=DEFAULT_CASE)
    e.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    e.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    e.add_argument(
        "--no-case",
        action="store_true",
        help="omit the case record (notes and leads). Rarely right: it is the "
        "one part nobody can rebuild by re-running the tool",
    )
    e.add_argument("--archive", type=Path, help="also zip it to this path")

    i = inner.add_parser("import", help="open a bundle somebody sent you")
    i.add_argument("path", type=Path, help="a bundle directory or .zip")
    i.add_argument("--into", type=Path, help="where to unpack a .zip")


def run(args: argparse.Namespace, render: Renderer) -> int:
    command = getattr(args, "bundle_cmd", None)
    if command == "export":
        return _export(args)
    if command == "import":
        return _import(args)
    if getattr(args, "path", None) is None:
        print("error: give a bundle path, or use `bundle export` / `bundle import`")
        return 2
    return _show(args)


def _export(args: argparse.Namespace) -> int:
    bundle = Bundle.create(args.dest, title=args.title, subject=args.subject, notes=args.notes)
    print(f"bundle at {bundle.path}")

    if args.no_case:
        # Said out loud. A bundle that quietly lacks the reasoning looks
        # identical to one that never had any.
        print("  case        omitted by --no-case; the notes and leads are NOT here")
    else:
        try:
            size = bundle.attach_case(args.case)
        except BundleError as exc:
            print(f"error: {exc}")
            return 2
        print(f"  case        {size:,} bytes from {args.case}")

    if Path(args.audit).is_file():
        print(f"  audit       {bundle.attach_audit(args.audit):,} bytes")
    else:
        print(f"  audit       none at {args.audit}")

    if Path(args.cache).is_file():
        from ...transport.cache import Cache

        size = bundle.attach_cache(Cache(args.cache))
        print(f"  queries     {size:,} bytes --- this bundle is replayable")
    else:
        # The difference between a bundle somebody can check and one they must
        # take on trust. Not a footnote.
        print(f"  queries     none at {args.cache}")
        print("              NOT replayable: the reader cannot check the")
        print("              findings against the responses they came from")

    if args.archive:
        print(f"\narchived to {bundle.archive(args.archive)}")
    return 0


def _import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        if path.suffix == ".zip":
            bundle = Bundle.unpack(path, args.into or path.with_suffix(""))
            print(f"unpacked to {bundle.path}")
        else:
            bundle = Bundle.open(path)
    except BundleError as exc:
        print(f"error: {exc}")
        return 2

    summary = bundle.summary()
    print(f"\n{summary['title'] or '(untitled)'}")
    print(f"  subject     {summary['subject'] or '-'}")
    print(f"  created     {summary['created_at']}")
    print(f"  tool        chainscope {summary['tool_version']}")
    print(f"  analyses    {summary['analyses']}")

    case = bundle.case_db()
    if case:
        print(f"  case        {case}")
        print(f"              read it with `chainscope lead list --case {case}`")
    else:
        print("  case        none in this bundle --- findings without the notes")
        print("              and leads that produced them")

    if summary["replayable"]:
        print("  replayable  yes --- rerun offline against the bundled queries")
    else:
        print("  replayable  NO --- no query cache, so nothing here can be")
        print("              independently checked")
    return 0 if summary["replayable"] else 1


def _show(args: argparse.Namespace) -> int:
    try:
        b = Bundle.open(args.path)
    except BundleError as exc:
        print(f"error: {exc}")
        return 2

    s = b.summary()
    print(f"{s['title'] or '(untitled)'}")
    print(f"  subject     {s['subject'] or '-'}")
    print(f"  created     {s['created_at']}")
    print(f"  tool        chainscope {s['tool_version']}")
    print(f"  analyses    {s['analyses']}")
    print(f"  findings    {s['total_findings']}")
    print(f"  warnings    {s['total_warnings']}")

    if s["replayable"]:
        print("  replayable  yes -- queries are bundled; this can be rerun offline")
    else:
        print("  replayable  NO -- no query cache attached, so the findings")
        print("              here cannot be independently verified")

    if b.notes:
        print(f"\nnotes\n  {b.notes}")

    print("\nanalyses")
    for i, entry in enumerate(b.results):
        flag = f"  ({entry.get('warnings')} warning(s))" if entry.get("warnings") else ""
        print(f"  {i}. {entry.get('analyzer')}  {entry.get('findings')} finding(s){flag}")

    if args.archive:
        dest = b.archive(args.archive)
        print(f"\narchived to {dest}")

    return 0 if s["replayable"] else 1
