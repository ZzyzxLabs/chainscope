"""``chainscope lead`` --- somewhere to look next, and what became of it.

The command that makes :mod:`chainscope.osint.leads` exist. That module has
defined what a lead is since early on, carefully: never an attribution, always
carrying the specific step that would settle it. Nothing called it. §2 of
`docs/needs.md` is blunt about what that means --- a technique nobody can reach
does not exist --- and this is the reach.

Three verbs, because an investigation has three moments: somebody notices
something (``add``), somebody wants to know what is outstanding (``list``), and
somebody checks one and writes down what they found (``settle``).

The fourth thing a tool like this usually does --- delete a lead that turned out
to be nothing --- is deliberately absent. A refuted lead is the record that
somebody already looked, which is what stops the next analyst repeating the
search and, in a shared case, stops two people doing it at once.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...case.leads import LeadStore, Verdict
from ...case.log import whoami
from ...osint.leads import TEXT_KEYS, Lead
from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="record and settle leads: places to look next")
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["add", "list", "settle"],
        help="add a lead, list them, or record what checking one found",
    )
    p.add_argument("target", nargs="?", help="address (add/list) or lead id (settle)")
    p.add_argument("--kind", "-k", help=f"one of: {', '.join(sorted(set(TEXT_KEYS.values())))}")
    p.add_argument("--value", "-v", help="the handle, domain, or address found")
    p.add_argument(
        "--source", "-s", help="where this was read, precisely enough to fetch again"
    )
    p.add_argument(
        "--asserted-by",
        help="who put it there. Not necessarily the address --- that gap is the lead's risk",
    )
    p.add_argument(
        "--verify-by",
        help="what would confirm it. Required: a lead without one is a rumour with a schema",
    )
    p.add_argument(
        "--verdict",
        choices=[v.value for v in Verdict if v is not Verdict.OPEN],
        help="for settle: what checking it found",
    )
    p.add_argument("--why", help="for settle: why. Required --- see the refusal it produces")
    p.add_argument("--open", action="store_true", help="list only what is still outstanding")
    p.add_argument("--case", type=Path, default=Path(".chainscope/case.db"))
    p.add_argument("--analyst", help="who is filing this; defaults to the environment")


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def run(args: argparse.Namespace, render: Renderer) -> int:
    analyst = args.analyst or whoami().name
    store = LeadStore(args.case)
    try:
        if args.action == "add":
            return _add(args, store, analyst)
        if args.action == "settle":
            return _settle(args, store, analyst)
        return _list(args, store)
    finally:
        store.close()


def _add(args: argparse.Namespace, store: LeadStore, analyst: str) -> int:
    missing = [
        flag
        for flag, value in (
            ("target (the address)", args.target),
            ("--kind", args.kind),
            ("--value", args.value),
            ("--source", args.source),
        )
        if not value
    ]
    if missing:
        _err(f"a lead needs {', '.join(missing)}")
        return 2
    try:
        lead = Lead(
            address=args.target,
            kind=args.kind,
            value=args.value,
            source=args.source,
            # Defaulted, not omitted. "Whoever wrote it" is honest and is what
            # a manually-filed lead usually knows; leaving it empty would fail
            # the type's own check for a reason the user cannot act on.
            asserted_by=args.asserted_by or "whoever wrote the source",
            verify_by=args.verify_by or _default_verification(args.kind),
        )
    except ValueError as exc:
        _err(str(exc))
        return 2

    lead_id, was_new = store.add(lead, analyst)
    if not was_new:
        # Filed before. Said plainly rather than reported as a fresh success,
        # because "already known, and already refuted" is the single most useful
        # thing this command can tell somebody about to spend an hour on it.
        existing = store.get(lead_id)
        print(f"lead {lead_id} was already on file: {existing}")
        if not existing.is_open:
            print("  Somebody has already checked this. Read the reason before repeating it.")
        return 0
    print(f"lead {lead_id} recorded for {args.target}")
    print(f"  verify by: {lead.verify_by}")
    print("  This is not an attribution. It is somewhere to look.")
    return 0


def _default_verification(kind: str) -> str:
    from ...osint.leads import _VERIFY

    return _VERIFY.get(
        kind,
        "state what would confirm this; a lead without a verification step "
        "will be read as a finding",
    )


def _settle(args: argparse.Namespace, store: LeadStore, analyst: str) -> int:
    if not args.target or not args.target.isdigit():
        _err("settle needs a lead id; run `chainscope lead list` to see them")
        return 2
    if not args.verdict:
        _err(
            "--verdict is required: one of "
            + ", ".join(v.value for v in Verdict if v is not Verdict.OPEN)
        )
        return 2
    try:
        record = store.settle(int(args.target), Verdict(args.verdict), args.why or "", analyst)
    except ValueError as exc:
        _err(str(exc))
        return 2
    print(f"lead {record.id} settled: {record.verdict.value}")
    print(f"  {record.reason}")
    print("  Kept, not deleted --- the record that somebody checked is the point.")
    return 0


def _list(args: argparse.Namespace, store: LeadStore) -> int:
    records = store.open_leads(args.target) if args.open else store.leads(args.target)
    counts = store.summary()

    if not records:
        where = f" for {args.target}" if args.target else ""
        print(f"no leads{where}")
        # Not silence. "Nothing recorded" and "nothing to find" are different,
        # and only the first is what this knows.
        print("  Nothing has been filed. That is a statement about this case file,")
        print("  not about the addresses in it.")
        return 1

    for record in records:
        print(record)
        if record.is_open:
            print(f"      {record.source}")

    print()
    print(
        "  "
        + ", ".join(f"{n} {name}" for name, n in counts.items() if n)
        + f"  (of {sum(counts.values())})"
    )
    if counts.get("open"):
        print("  Open leads are unverified by definition. None of them is a finding.")
    return 0
