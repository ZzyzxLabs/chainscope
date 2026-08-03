"""``chainscope tag`` --- put labels into the store.

Two shapes, because labelling happens two ways and forcing one into the other
makes both worse.

*One address, right now.* You have just worked out what something is and want
it recorded before you lose the thread. That has to be a single line with no
file and no ceremony.

*A file you already have.* Somebody's exported spreadsheet, an explorer dump, a
list a colleague sent. That has to accept the columns it already has rather
than a schema nobody has read.

Both refuse a label with no source, because
:class:`~chainscope.core.attribution.Attribution` cannot be constructed without
one, and both refuse a low-confidence claim with no rationale for the same
reason. That is the point at which "I'll write down why later" stops being an
option --- which is the only point at which it works.

Bulk import is a dry run unless ``--apply`` is passed. Undoing thirty thousand
mislabelled addresses costs far more than not writing them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...attribution.ingest import ImportError_, ingest_file, plan_import
from ...case.log import whoami
from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ChainId, resolve
from ...render.base import Renderer
from ...store.sqlite import SqliteStore

__all__ = ["add_parser", "run"]


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="record attribution for an address, or import a label file")
    p.add_argument(
        "target",
        nargs="?",
        help="an address to label, or a .csv/.json/.jsonl file to import",
    )
    p.add_argument("--label", "-l", help="what it is, e.g. 'Binance hot wallet 14'")
    p.add_argument(
        "--category",
        "-t",
        default="service",
        help="one of: " + ", ".join(sorted(c.value for c in Category)),
    )
    p.add_argument(
        "--confidence",
        "-C",
        default="medium",
        help="certain, high, medium, low, speculative (or 0-4)",
    )
    p.add_argument(
        "--source",
        "-s",
        help="where this came from. Required --- an unattributed claim is "
        "exactly what the type system exists to prevent",
    )
    p.add_argument(
        "--why",
        "-w",
        default="",
        help="rationale. Required for low and speculative confidence",
    )
    p.add_argument(
        "--method",
        "-m",
        default=None,
        help="list, label, onchain, heuristic, inference, manual. Defaults to "
        "'manual' when tagging one address and 'list' when importing a file",
    )
    p.add_argument("--chain", "-c", help="CAIP-2 id or EVM chain number")
    p.add_argument(
        "--analyst",
        help="who is making this claim. Defaults to $CHAINSCOPE_ANALYST, then "
        "git's user.email. Distinct from --source, which is where you learnt it",
    )
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually write the import. Without it, it is planned and reported only",
    )
    p.add_argument("--limit", type=int, default=10, help="how many errors/conflicts to show")


def run(args: argparse.Namespace, render: Renderer) -> int:
    if not args.target:
        _err("give an address to label, or a file to import")
        return 2
    if not args.source:
        _err(
            "--source is required. A label whose origin nobody recorded becomes, "
            "three tools later, a fact nobody can trace back."
        )
        return 2

    target = Path(args.target)
    if target.suffix.lower() in (".csv", ".json", ".jsonl", ".ndjson"):
        return _import_file(args, render, target)
    return _tag_one(args, render)


def _chain(raw: str | None) -> ChainId | None:
    """Resolve ``--chain``, distinguishing absent from invalid.

    Absent is a real answer: a claim with no chain applies everywhere, which is
    how sanctions lists are published. Invalid is not, and the two used to
    collapse into the same ``None`` --- so ``--chain bsc``, the obvious thing to
    type, produced a claim applying to *every* chain rather than to BSC. So did
    a typo. Only a bare number or a full CAIP-2 string worked.

    The failure is quiet and it spreads: the graph layer takes care not to put a
    BSC label on the Ethereum address sharing its hex, and a claim scoped to
    nothing defeats that from the other end.

    :func:`~chainscope.core.chainid.resolve` knows the aliases and raises on
    anything it does not, which is what a CLI boundary is for.
    """
    if raw is None or not raw.strip():
        return None
    return resolve(raw)


def _tag_one(args: argparse.Namespace, render: Renderer) -> int:
    if not args.label:
        _err("--label is required when tagging an address")
        return 2

    try:
        attribution = Attribution(
            label=args.label,
            category=Category(args.category),
            confidence=_confidence(args.confidence),
            # A person typed this at a prompt, so it is their judgement.
            method=Method(args.method or "manual"),
            source=args.source,
            address=args.target,
            chain=_chain(args.chain),
            rationale=args.why,
            analyst=_analyst(args),
        )
    except ValueError as exc:
        # Includes the two refusals that matter: no source, and a low-confidence
        # claim with no rationale.
        _err(str(exc))
        return 2

    store = SqliteStore(args.store)
    try:
        store.put_attributions([attribution])
    finally:
        store.close()

    print(
        f"recorded {attribution.label} for {args.target} "
        f"({attribution.category.value}, {attribution.confidence.name.lower()}, "
        f"source: {attribution.source})"
    )
    if attribution.analyst:
        print(f"  asserted by {attribution.analyst}")
    else:
        print(
            "  no analyst recorded. Set CHAINSCOPE_ANALYST or git user.email so "
            "a\n  shared case can say who made this claim.",
            file=sys.stderr,
        )
    return 0


def _analyst(args: argparse.Namespace) -> str:
    """Who is asserting this, when a person is.

    Only for the single-address path. A bulk import is a list somebody
    compiled, not a judgement each row was made with, and stamping every row of
    a thirty-thousand-line CSV with the name of whoever ran the import would
    make the field mean nothing on the rows where it matters.

    The OS account is not used. A machine login is not authorship, and a claim
    signed with one is worse than a claim signed with nothing --- it looks
    attributed.
    """
    if args.analyst:
        return str(args.analyst).strip()
    who = whoami()
    return who.name if who.is_chosen else ""


def _confidence(raw: str) -> Confidence:
    """Parse ``--confidence``, and name the options when it is wrong.

    ``Confidence[text.upper()]`` raises `KeyError('HGIH')` --- an uncaught
    traceback from a one-character typo, at the boundary whose job is to turn a
    typed argument into either a value or a sentence. `_tag_one` catches
    `ValueError` and would have printed something; `KeyError` is not one, so it
    escaped both call sites.

    The numeric form stays: `--confidence 3` appears in scripts. Out of range
    now reads as out of range rather than as an enum's internal complaint.
    """
    text = raw.strip().lower()
    names = ", ".join(c.name.lower() for c in Confidence)
    if text.isdigit():
        try:
            return Confidence(int(text))
        except ValueError:
            raise ValueError(
                f"--confidence {raw!r} is out of range. "
                f"Use 0-{max(c.value for c in Confidence)}, or a name: {names}"
            ) from None
    try:
        return Confidence[text.upper()]
    except KeyError:
        raise ValueError(f"unknown --confidence {raw!r}. Use one of: {names}") from None


def _import_file(args: argparse.Namespace, render: Renderer, path: Path) -> int:
    store = SqliteStore(args.store) if args.apply else None
    try:
        if store is not None:
            plan = ingest_file(
                path,
                store,
                source=args.source,
                chain=_chain(args.chain),
                apply=True,
                default_confidence=_confidence(args.confidence),
                # A bulk file is a list somebody compiled, not a
                # judgement made per row while reading evidence.
                # Recording it as MANUAL would overstate every row in it.
                default_method=Method(args.method or "list"),
            )
        else:
            plan = plan_import(
                path,
                source=args.source,
                chain=_chain(args.chain),
                default_confidence=_confidence(args.confidence),
                default_method=Method(args.method or "list"),
            )
    except ImportError_ as exc:
        _err(str(exc))
        return 1
    except ValueError as exc:
        # `--chain bsx` and `--confidence hgih` reach here. Both are the caller
        # mistyping an argument, which is exit 2 and the same code `_tag_one`
        # returns for them --- neither is an import that ran and failed, and a
        # script has to be able to tell those apart. Before this they came back
        # as a traceback, which is neither.
        _err(str(exc))
        return 2
    finally:
        if store is not None:
            store.close()

    summary = plan.summary()
    print(f"{path}: {summary['ready']} labels ready, {summary['errors']} rejected")
    for category, count in summary["by_category"].items():
        print(f"  {category:<14} {count}")
    if plan.duplicates:
        print(f"  {plan.duplicates} duplicate rows within the file, ignored")

    if plan.errors:
        print("")
        print(f"rejected ({len(plan.errors)}):")
        for error in plan.errors[: args.limit]:
            print(f"  {error}")
        if len(plan.errors) > args.limit:
            print(f"  … and {len(plan.errors) - args.limit} more")

    if plan.conflicts:
        print("")
        print(f"disagrees with what is already stored ({len(plan.conflicts)}):")
        for conflict in plan.conflicts[: args.limit]:
            print(f"  {conflict}")
        print("  Both are kept. The resolver decides at read time.")

    if args.apply:
        print("")
        print(f"wrote {summary['ready']} attributions to {args.store}")
    else:
        print("")
        print("dry run --- nothing written. Re-run with --apply to commit.")
    return 0 if not plan.errors else 1
