"""``chainscope note`` --- write down the reasoning, not just the conclusion.

Every claim this package stores carries a source. None of them carried a
*narrative*: why this address and not that one, what was ruled out, what
somebody is still waiting to hear back about. That reasoning normally lives in
a person's head until the case is over, at which point it is reconstructed
badly or not at all.

Four kinds, because a narrative where everything is one undifferentiated kind
cannot answer the two questions a report has to ask of it --- what is still
open, and what has been withdrawn:

    chainscope note observation "0xabc funds three of the four drainers"
    chainscope note decision    "not tracing past the CEX deposit; terminal"
    chainscope note question    "who paid the gas for the first probe?"
    chainscope note correction  "the 3rd hop is a router, not the thief" --supersedes 4

**Append-only.** There is no edit and no delete. A log that silently reflects
only the final position cannot be told apart from one that was right the first
time, and "I thought X, then found Y" is the substance of an investigation
rather than an embarrassment to be tidied away. A correction has to name what
it replaces, so that "that was wrong" points at something.

**It goes in `case.db`, not the store.** The store is derived and disposable by
design --- rebuildable from the cache, and `clear()` exists. Nothing a person
wrote is either of those things.

**Exit codes answer the question that was asked.** `--open` is asking *is
anything unresolved*, so it exits non-zero when something is --- the same
convention as `chainscope request list`, where a script must not read a case
with unanswered questions as a finished one. Listing exits non-zero only when
there is nothing to list, because there the question is *is there a record*.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...case.log import CaseLog, Note, NoteKind, whoami
from ...render.base import Renderer

__all__ = ["add_parser", "run"]

_KINDS = [k.value for k in NoteKind]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="record a case note: an observation, decision, or question")
    p.add_argument(
        "kind",
        nargs="?",
        choices=_KINDS,
        help="observation (what was seen), decision (what was chosen and why), "
        "question (open), correction (replaces an earlier note)",
    )
    p.add_argument("body", nargs="?", help="the note itself")
    p.add_argument(
        "--about",
        "-a",
        default="",
        help="address or tx this attaches to. Omit for a note about the case",
    )
    p.add_argument("--chain", "-c", help="chain of --about, when it has one")
    p.add_argument(
        "--supersedes",
        type=int,
        help="the note id this correction replaces. Required for a correction",
    )
    p.add_argument("--case", type=Path, default=Path(".chainscope/case.db"))
    p.add_argument("--list", "-L", action="store_true", help="show the log instead of adding")
    p.add_argument(
        "--open",
        action="store_true",
        help="show only questions nothing has answered",
    )
    p.add_argument(
        "--analyst", help="override who this is from. Defaults to $CHAINSCOPE_ANALYST"
    )


def run(args: argparse.Namespace, render: Renderer) -> int:
    log = CaseLog(args.case)
    try:
        if args.open:
            questions = log.open_questions()
            if not questions:
                print("no open questions")
                return 0
            _show(questions, log, empty="")
            # Non-zero *because* there are open questions. `--open` asks
            # whether anything is unresolved, and a script must not read
            # silence from it as a finished case.
            return 1
        if args.list or not args.kind:
            if not args.kind and not args.list:
                # Nothing to add and nothing asked for: show the log rather than
                # print usage. Somebody typing `note` alone wants to read it.
                pass
            return _show(log.notes(subject=args.about), log, empty="no notes yet")
        return _add(args, log)
    finally:
        log.close()


def _add(args: argparse.Namespace, log: CaseLog) -> int:
    if not args.body:
        print(f'give the note text: chainscope note {args.kind} "..."', file=sys.stderr)
        return 2

    # Stripped before the test: `--analyst "  "` is not somebody naming
    # themselves, and it recorded a note authored by an empty string.
    stated = str(args.analyst or "").strip()
    who = whoami()
    name = stated or who.name
    origin = "flag" if stated else who.source

    try:
        note = Note(
            at=datetime.now(timezone.utc),
            analyst=name,
            identified_by=origin,
            kind=NoteKind(args.kind),
            body=args.body,
            subject=args.about,
            chain=args.chain,
            supersedes=args.supersedes,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        note_id = log.add(note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    where = f" about {note.subject}" if note.subject else ""
    print(f"note {note_id} recorded ({note.kind.value}{where}) --- {name}")
    if origin == "os":
        # Said once, at the point it can still be fixed. A case shared between
        # two people needs to know which names somebody chose.
        print(
            "  authorship is your OS account, which may mean nothing to a "
            "colleague.\n  Set CHAINSCOPE_ANALYST, or configure git user.email.",
            file=sys.stderr,
        )
    if note.kind is NoteKind.QUESTION:
        print("  open until a later note supersedes it: chainscope note --open")
    return 0


def _show(notes: list[Note], log: CaseLog, *, empty: str) -> int:
    if not notes:
        print(empty)
        return 1

    replaced = log.superseded()
    for note in notes:
        stamp = note.at.strftime("%Y-%m-%d %H:%M")
        mark = "~" if note.id in replaced else " "
        head = f"{mark}{note.id:>4}  {stamp}  {note.kind.value:<11} {note.analyst}"
        if note.subject:
            head += f"  [{note.subject}]"
        print(head)
        for line in note.body.splitlines() or [""]:
            print(f"        {line}")
        if note.supersedes:
            print(f"        (replaces note {note.supersedes})")

    if replaced & {n.id for n in notes}:
        print(
            "\n~  superseded by a later correction. Kept: what was believed and "
            "when it changed\n   is the record, and a log showing only the final "
            "position looks like one that\n   was right the first time."
        )
    return 0
