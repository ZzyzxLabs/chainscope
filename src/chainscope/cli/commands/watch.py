"""``chainscope watch`` --- run the rules that were only ever definitions.

The predicates in :mod:`chainscope.watch` were written as pure functions and
tested that way, and nothing ever ran them. Half of forensic work is being told
when something moves rather than looking back at what already did, so a rule
engine with no runner is the wrong half.

This is the runner: read watches from a file, evaluate them over a block range,
report what fired. Once, or on a loop.

**A watch that has not run since block N is not a watch that found nothing.**
The state file records where each rule got to, and a run that cannot reach a
provider says so rather than advancing the mark --- otherwise the gap silently
becomes a period nobody watched and nobody knows nobody watched.

Delivery is deliberately absent. The events go to stdout and an exit code, so
`cron`, `systemd`, a shell loop, or a person can decide what happens next.
Building notification into it would mean choosing somebody's alerting stack for
them, and this has no business doing that.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["add_parser", "load_watches", "run"]

#: Where each watch got to, so a restart does not re-report or skip.
DEFAULT_STATE = Path(".chainscope/watch-state.json")


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="evaluate watch rules over new blocks")
    p.add_argument("rules", type=Path, help="JSON file of watch definitions")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--since", type=int, help="start block; overrides saved state")
    p.add_argument("--until", type=int, help="end block; defaults to the store's newest")
    p.add_argument(
        "--every",
        type=int,
        metavar="SECONDS",
        help="loop instead of running once. Ctrl-C to stop",
    )
    p.add_argument("--format", "-F", default="text", choices=["text", "json"], dest="shape")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate without advancing the saved position",
    )


def load_watches(path: Path) -> list[Any]:
    """Read watch definitions.

    A rule that will not parse **stops the run**. The alternative --- skipping it
    and carrying on --- produces a monitor that is quietly watching less than it
    was told to, which is the failure this whole package is arranged against and
    is worse here than anywhere else: nobody looks at a monitor that is not
    complaining.
    """
    from ...watch.base import AmountOver, CounterpartyIn, TouchesCategory, Watch

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} should hold a list of watches, got {type(raw).__name__}")

    watches = []
    for i, entry in enumerate(raw, 1):
        try:
            kind = entry["when"]["kind"]
            args = {k: v for k, v in entry["when"].items() if k != "kind"}
            if kind == "amount_over":
                predicate: Any = AmountOver(int(args["raw"]))
            elif kind == "counterparty_in":
                predicate = CounterpartyIn(frozenset(str(a).lower() for a in args["addresses"]))
            elif kind == "touches_category":
                predicate = TouchesCategory(args["category"])
            elif kind == "any_of":
                raise ValueError(
                    "any_of must be built in Python; JSON nesting is not supported"
                )
            else:
                raise ValueError(f"unknown predicate {kind!r}")

            watches.append(
                Watch(
                    name=entry["name"],
                    subject=entry["subject"],
                    predicate=predicate,
                    chain=_chain(entry.get("chain")),
                    direction=entry.get("direction", "both"),
                )
            )
        except Exception as exc:
            raise ValueError(f"watch {i} in {path}: {exc}") from exc

    return watches


def _chain(raw: str | None) -> Any:
    from ...core.chainid import ETHEREUM, resolve

    return resolve(raw) if raw else ETHEREUM


def _state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A corrupt state file must not be silently reset: that would re-scan
        # from zero or skip a range, and neither is visible in the output.
        raise ValueError(f"{path} is not readable JSON; delete it to start over") from None
    return {k: int(v) for k, v in data.items()} if isinstance(data, dict) else {}


def run(args: argparse.Namespace, render: Renderer) -> int:
    if not args.store.exists():
        print(f"no store at {args.store}", file=sys.stderr)
        return 1
    try:
        watches = load_watches(args.rules)
    except (OSError, ValueError) as exc:
        print(f"could not read rules: {exc}", file=sys.stderr)
        return 2
    if not watches:
        print(f"{args.rules} defines no watches", file=sys.stderr)
        return 2

    print(f"{len(watches)} watch(es) over {args.store}", file=sys.stderr)
    while True:
        fired = _once(args, watches)
        if args.every is None:
            return 0 if fired else 1
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            return 0


def _once(args: argparse.Namespace, watches: list[Any]) -> int:
    from ...store.sqlite import SqliteStore
    from ...watch.base import evaluate

    state = _state(args.state)
    store = SqliteStore(args.store)
    events: list[Any] = []
    try:
        # Asked of the data rather than of StoreStats, which does not carry it.
        # `until` has to be a real block the store has seen: defaulting to the
        # chain tip would advance every watch past blocks that were never
        # ingested, and the gap would look watched.
        row = store._conn.execute("SELECT MAX(block) FROM transfers").fetchone()
        newest = int(row[0]) if row and row[0] is not None else 0
        until = args.until if args.until is not None else newest
        for watch in watches:
            since = args.since if args.since is not None else state.get(watch.name, 0)
            if since >= until:
                continue
            try:
                events.extend(evaluate(watch, store, since, until))
            except Exception as exc:
                # The position is *not* advanced. A watch that could not run is
                # not a watch that found nothing, and moving the mark would turn
                # the gap into a period nobody watched and nobody knows about.
                print(f"  {watch.name}: could not evaluate: {exc}", file=sys.stderr)
                continue
            state[watch.name] = until
    finally:
        store.close()

    if args.shape == "json":
        print(json.dumps([_event_dict(e) for e in events], indent=2))
    else:
        for event in events:
            print(f"[{event.severity.name}] {event.watch}: {event.reason}")
        if not events:
            print("nothing fired", file=sys.stderr)

    if not args.dry_run:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return len(events)


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "watch": event.watch,
        "subject": event.subject,
        "chain": str(event.chain),
        "severity": event.severity.name,
        "reason": event.reason,
        "since": event.since,
        "until": event.until,
        "tx": getattr(getattr(event.transfer, "tx", None), "hash", None),
    }
