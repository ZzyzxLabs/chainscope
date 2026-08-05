"""``chainscope screen`` --- can this money be accepted, and on whose say-so.

The reach for :mod:`chainscope.risk`. That package has defined exposure,
policy and decision carefully since it was written, and nothing had ever built
one of its types from a real transfer --- which made it a schema rather than a
control. §2 of `docs/needs.md` is blunt about what that means.

The command answers one question and refuses to answer it as a number. What
comes back is an action (`allow`, `hold`, `enhanced_kyc`, `escalate`, `reject`,
`report`), the id of the rule that produced it, the sentence that rule was
written with, the exposures underneath, and --- the part a score cannot do ---
**what would have had to be absent for the answer to differ**. "This is a hold
because of one OFAC tag on an address three hops away, and without it the
answer is allow" can be argued with, taken to the customer, or acted on.

Reading only. The screen is built from what the store holds and nothing here
touches the network, because a screening decision made from a fetch is a
decision whose inputs nobody can reproduce six months later. Where the store is
short, that shows up as `unreachable_sources`, which makes the screen
incomplete, which makes `allow` unreachable --- so an unread chain never
emerges from this command as a clean one. Use `chainscope investigate` first if
the case is empty.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...attribution.build import DEFAULT_LABEL_DIR, resolver_for
from ...case.log import whoami
from ...core.chainid import resolve
from ...render.base import Renderer
from ...risk import load_policy, screen, starter_policy
from ...risk.decision import Decision
from ...risk.policy import Policy, PolicyError
from ...risk.screener import DEFAULT_HOPS, shape_signals
from ...store.sqlite import SqliteStore

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(
        name,
        help="screen a deposit: what it is exposed to, and what a policy says to do",
    )
    p.add_argument("address", help="the address value arrived at")
    p.add_argument("--chain", "-c", default="eth")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument(
        "--policy",
        type=Path,
        help="a policy YAML file. Without one, the `starter` policy is used and "
        "every decision says so",
    )
    p.add_argument(
        "--hops",
        type=int,
        default=DEFAULT_HOPS,
        help="how far back to walk. Stopping early makes the screen incomplete, "
        "which makes `allow` unreachable --- it cannot produce a false clearance",
    )
    p.add_argument(
        "--asset",
        help="the token contract to follow, or `native` for the chain's own "
        "coin. Omitted, the largest inbound quantity is used and the screen "
        "says what it passed over --- ranking assets without a price is not a "
        "value judgement",
    )
    p.add_argument(
        "--at",
        help="when the value arrived (ISO 8601). Every time-sensitive check is "
        "asked against this and never against now. Defaults to now",
    )
    p.add_argument(
        "--no-signals",
        action="store_true",
        help="skip the behavioural signals. Attribution only, which is what a "
        "screen answers 'clean' with on the day of an incident",
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABEL_DIR,
        help="where the label datasets live. Configured, never derived from "
        "--store: guessing it once turned every source off while the banner "
        "went on naming them",
    )
    p.add_argument("--analyst", help="who is running this; defaults to the environment")
    p.add_argument(
        "--attest",
        default="",
        help="what the analyst is attesting to. Recorded on the decision",
    )


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def run(args: argparse.Namespace, render: Renderer) -> int:
    try:
        chain = resolve(args.chain)
    except ValueError as exc:
        _err(str(exc))
        return 2

    if not args.store.exists():
        # Named, not guessed at. An empty screen from a store that does not
        # exist is the exact shape this package refuses: an absence that reads
        # like a result.
        _err(
            f"no store at {args.store}. A screen reads what has already been "
            f"fetched; run `chainscope investigate {args.address} -c {args.chain}` "
            f"first, or point --store at the case you mean"
        )
        return 2

    try:
        policy = load_policy(args.policy) if args.policy else starter_policy()
    except (PolicyError, OSError) as exc:
        _err(str(exc))
        return 2

    at = _when(args.at)
    if at is None:
        _err(f"--at is not a timestamp I can read: {args.at!r}")
        return 2

    store = SqliteStore(args.store)
    try:
        resolver = resolver_for(args.labels)
        signals = () if args.no_signals else shape_signals(store, args.address, chain)
        result = screen(
            store,
            args.address,
            chain,
            resolver=resolver,
            asset=args.asset,
            at=at,
            hops=args.hops,
            signals=signals,
        )
    finally:
        store.close()

    decision = policy.decide(
        result,
        at=datetime.now(timezone.utc),
        analyst=args.analyst or whoami().name,
        attestation=args.attest,
    )
    _print(decision, policy, custom=bool(args.policy))
    return 0 if decision.action.value == "allow" else 1


def _when(raw: str | None) -> datetime | None:
    """Parse `--at`, identically on every Python this supports.

    The `Z` is rewritten by hand because `fromisoformat` learned to read it in
    3.11 and CI runs 3.10 --- so `2026-08-01T00:00:00Z` would be a valid
    timestamp on a developer's machine and a usage error on the build. A flag
    that means two things depending on the interpreter is worse than one that
    means nothing.
    """
    if not raw:
        return datetime.now(timezone.utc)
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _print(decision: Decision, policy: Policy, *, custom: bool) -> None:
    print(decision.explain())

    if decision.screen.notes:
        # Printed, not carried silently. The notes are where the screen says
        # what it narrowed --- one asset out of three, seventy-nine dust edges
        # folded --- and a reader who is not told what was set aside has been
        # handed a cleaned-up picture with no way to judge the cleaning.
        print()
        print("  what this screen set aside")
        for note in decision.screen.notes:
            print(f"    - {note}")

    if not custom:
        # Said every time, because the alternative is somebody reading a
        # `starter` decision as though an institution had written it.
        print()
        print("  No policy file was given, so this ran under `starter` v1 --- a")
        print("  placeholder whose thresholds nobody at your institution has")
        print("  reviewed. The policy name and version are on the decision, so a")
        print("  report produced from it says as much. Write your own and pass")
        print("  --policy; `chainscope screen --help` names the shape.")

    if not decision.screen.complete:
        print()
        print("  This screen is incomplete. `allow` is unreachable by construction")
        print("  while it is --- an absent answer is not a clean one.")
        for gap in decision.screen.unreachable_sources:
            print(f"    - {gap}")
