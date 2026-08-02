"""``chainscope request`` --- the clock on what was asked of an exchange.

Tracing ends at a custodial deposit. The case does not: what happens next is a
KYC request, a freeze request, a preservation letter, and a wait. This is the
part of an investigation that has a deadline, and it was the one thing here with
nowhere to live.

    chainscope request send Binance --kind freeze --about 0xabc… \\
        --due 2026-08-14 --ref TICKET-9912
    chainscope request update 3 answered --note "12.4 ETH held; KYC to follow"
    chainscope request list --open

**The clock is the output.** A list of requests that does not show elapsed time
is a list. `list` leads with age and marks anything past its deadline, because
the question this exists to answer is *what has been sitting there.*

**Overdue is computed, never stored.** A stored one is correct only if somebody
remembered to run a sweep.

**Silence is not refusal.** A request nobody has answered and one somebody
declined are different facts leading to different next moves; only the second
can be escalated against.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...case.correspondence import Ledger, Request, RequestEvent, RequestKind, Status
from ...case.log import whoami
from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="track KYC, freeze, and records requests sent to exchanges")
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["send", "update", "list"],
        help="send a new request, record what came back, or show the ledger",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="the counterparty for `send`, the request id for `update`",
    )
    p.add_argument(
        "status",
        nargs="?",
        choices=[s.value for s in Status],
        help="for `update`: acknowledged, partial, answered, refused, withdrawn",
    )
    p.add_argument(
        "--kind",
        "-k",
        default="kyc",
        choices=[k.value for k in RequestKind],
        help="kyc (who holds it), freeze (a hold), records, preservation",
    )
    p.add_argument("--about", "-a", default="", help="address or tx this concerns")
    p.add_argument("--chain", "-c")
    p.add_argument(
        "--sent",
        help="when it actually went out (YYYY-MM-DD). Defaults to now --- set it "
        "when recording after the fact, or the clock starts at the wrong moment",
    )
    p.add_argument("--due", help="deadline (YYYY-MM-DD). Overdue is computed from it")
    p.add_argument("--ref", default="", help="their ticket or case number")
    p.add_argument("--note", "-n", default="", help="what was asked, or what came back")
    p.add_argument("--case", type=Path, default=Path(".chainscope/case.db"))
    p.add_argument("--open", action="store_true", help="for `list`: only what is unresolved")
    p.add_argument("--analyst", help="override who this is from")


def run(args: argparse.Namespace, render: Renderer) -> int:
    ledger = Ledger(args.case)
    try:
        if args.action == "send":
            return _send(args, ledger)
        if args.action == "update":
            return _update(args, ledger)
        return _list(args, ledger)
    finally:
        ledger.close()


def _who(args: argparse.Namespace) -> tuple[str, str]:
    if args.analyst:
        return str(args.analyst).strip(), "flag"
    identity = whoami()
    return identity.name, identity.source


def _date(raw: str | None, field: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"--{field} should be YYYY-MM-DD, got {raw!r}") from None


def _send(args: argparse.Namespace, ledger: Ledger) -> int:
    if not args.target:
        print(
            'give the counterparty: chainscope request send "Binance" --kind freeze',
            file=sys.stderr,
        )
        return 2

    name, origin = _who(args)
    try:
        request = Request(
            counterparty=args.target,
            kind=RequestKind(args.kind),
            subject=args.about,
            chain=args.chain,
            sent_at=_date(args.sent, "sent") or datetime.now(timezone.utc),
            due_at=_date(args.due, "due"),
            reference=args.ref,
            analyst=name,
            identified_by=origin,
            body=args.note,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request_id = ledger.send(request)
    where = f" about {request.subject}" if request.subject else ""
    print(
        f"request {request_id} recorded: {request.kind.value} to {request.counterparty}{where}"
    )
    if request.due_at:
        days = (request.due_at - request.sent_at).days
        print(f"  due {request.due_at:%Y-%m-%d} ({days}d)")
    else:
        # Not an error, but worth saying: without one, nothing can report this
        # as overdue, and an untracked wait is how a case goes quiet.
        print("  no deadline set --- it will never show as overdue")
    print(f'  chainscope request update {request_id} answered --note "..."')
    return 0


def _update(args: argparse.Namespace, ledger: Ledger) -> int:
    if not args.target or not str(args.target).isdigit():
        print("give the request id: chainscope request update 3 answered", file=sys.stderr)
        return 2
    if not args.status:
        print(
            "give a status: " + ", ".join(s.value for s in Status),
            file=sys.stderr,
        )
        return 2

    name, origin = _who(args)
    try:
        event = RequestEvent(
            at=datetime.now(timezone.utc),
            status=Status(args.status),
            analyst=name,
            identified_by=origin,
            body=args.note,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ledger.record(int(args.target), event)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"request {args.target}: {event.status.value}")
    if event.status is Status.PARTIAL:
        print("  still open --- the remainder is the point")
    return 0


def _list(args: argparse.Namespace, ledger: Ledger) -> int:
    now = datetime.now(timezone.utc)
    requests = ledger.requests(subject=args.about, open_only=args.open)
    if not requests:
        print("no open requests" if args.open else "no requests recorded")
        return 1

    overdue = [r for r in requests if r.overdue_at(now)]
    # Oldest-open first: the list is read to find what has been sitting.
    for request in sorted(requests, key=lambda r: (-r.age_days(now), r.id)):
        status = request.status.value if request.status else "sent, nothing back"
        age = request.age_days(now)
        mark = "!" if request.overdue_at(now) else " "
        head = (
            f"{mark}{request.id:>4}  {age:>4}d  {request.kind.value:<12} "
            f"{request.counterparty}  --- {status}"
        )
        print(head)
        if request.subject:
            print(f"        about {request.subject}")
        if request.reference:
            print(f"        ref {request.reference}")
        if request.due_at and request.is_open:
            late = (now - request.due_at).days
            print(
                f"        due {request.due_at:%Y-%m-%d}"
                + (f" --- {late}d past" if late > 0 else f" --- {-late}d left")
            )
        for event in request.events:
            if event.body:
                print(f"        {event.at:%Y-%m-%d} {event.status.value}: {event.body}")

    if overdue:
        print(
            f"\n!  {len(overdue)} past a deadline and still open. Nothing here "
            f"chases them;\n   the clock is reported so it cannot be the thing "
            f"nobody was watching."
        )
    # Non-zero while anything is outstanding, so a script does not read a case
    # with unanswered requests as a finished one.
    return 0 if not [r for r in requests if r.is_open] else 1
