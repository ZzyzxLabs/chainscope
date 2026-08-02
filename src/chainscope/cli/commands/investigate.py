"""``chainscope investigate`` --- one address in, a place to start out.

Nine analyzers is nine ways to be stuck. Every one needs parameters somebody
has to already know: which deposit hashes went into the mixer, which address
was the source of the theft, what counts as a probe. The capability was there
and the first move was not, which is the worst shape for a tool --- it looks
complete and feels unusable.

So this takes an address and does the part a person does by hand at the start
of every case: run what applies, say what came back, and **name the next
command**. It is not clever. It is the sequence somebody types anyway, with the
arguments filled in.

**It never invents a lead.** Where nothing was found it says so, in the terms
that matter --- "no probing sequence" is not "they did not test the route", it
is "this window contains no run long enough to tell". Every suggestion it
prints is a command you can read before running.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["TooBusy", "add_parser", "run"]


class TooBusy(RuntimeError):
    """The address has more history than a provider will return at once.

    Not an error to swallow and not one to fail on. The honest response is a
    narrower question, and this carries the shape of it --- choosing a window
    here would produce a figure about two weeks and present it as the whole
    life of the address.
    """


@dataclass
class Step:
    """One thing tried, and what it produced."""

    name: str
    ran: bool = False
    findings: int = 0
    note: str = ""
    next_command: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        if not self.ran:
            return "—"
        return "●" if self.findings else "○"


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(
        name,
        help="start here: run what applies to an address and say what to do next",
    )
    p.add_argument("address")
    p.add_argument("--chain", "-c", default="eth")
    p.add_argument(
        "--single-source",
        action="store_true",
        help="ask one provider per enumeration instead of two. Faster and "
        "cheaper; a short answer then has nothing to disagree with it. The "
        "result says which it was either way",
    )
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument(
        "--depth",
        type=int,
        default=2,
        help="hops for the graph suggestion, not run here",
    )


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...analysis.base import Context
    from ...core.chainid import resolve
    from ...providers.base import Capability
    from ...providers.build import router_for

    address = args.address.strip()
    chain = resolve(args.chain)
    router, skipped = router_for(chain, corroborate=not args.single_source)
    ctx = Context(chain=chain, router=router, limits={})

    print(f"investigating {address} on {chain}\n")

    if not router.candidates(chain, Capability.ADDRESS_HISTORY):
        # The one failure worth stopping for: without history nothing below can
        # run, and listing nine things that cannot run is noise.
        print("nothing can run: no provider offers ADDRESS_HISTORY here.\n")
        for name, why in sorted(skipped.items()):
            print(f"  {name:12} {why}")
        print("\n  chainscope doctor        # what is reachable and what is missing")
        return 2

    steps = [
        _known(args, address),
        _leads(args, address),
        _temporal(ctx, address),
        _probing(ctx, address),
    ]

    width = max(len(s.name) for s in steps)
    for step in steps:
        print(f"  {step.icon} {step.name.ljust(width)}  {step.note}")
        for warning in step.warnings[:2]:
            print(f"      {warning}")
    print()

    followups = [s.next_command for s in steps if s.next_command]
    followups.append(
        f"chainscope graph {address} -c {args.chain} -f flow "
        f"--depth {args.depth} --out flow.html"
    )
    followups.append('chainscope sql "SELECT * FROM transfers LIMIT 20"')

    print("next:")
    for command in followups:
        print(f"  {command}")

    print(
        "\nNothing above is a conclusion. An empty result means this window "
        "held no\nevidence of that pattern --- not that the pattern is absent."
    )
    # Non-zero when nothing was found, so a script does not read silence as a
    # clean bill of health.
    return 0 if any(s.findings for s in steps) else 1


def _known(args: argparse.Namespace, address: str) -> Step:
    """What the case already says about this address."""
    step = Step(name="already labelled")
    if not args.store.exists():
        step.note = f"no store at {args.store} yet"
        step.next_command = (
            f'chainscope tag {address} -l "<what it is>" -t service '
            f'-C medium -s "<where you learnt it>"'
        )
        return step

    from ...store.sqlite import SqliteStore

    store = SqliteStore(args.store)
    try:
        claims = list(store.attributions(address))
    finally:
        store.close()

    step.ran = True
    step.findings = len(claims)
    if claims:
        best = max(claims, key=lambda c: int(c.confidence))
        step.note = f"{best.label} ({best.confidence.name.lower()}, {best.source})"
    else:
        step.note = "no claim in this store"
        step.next_command = (
            f'chainscope tag {address} -l "<what it is>" -t service '
            f'-C medium -s "<where you learnt it>"'
        )
    return step


def _leads(args: argparse.Namespace, address: str) -> Step:
    """Off-chain leads recorded against this address.

    Read from the store rather than fetched: a lead is somewhere a person looks
    next, and generating them mid-run would put unverified handles on the
    screen beside measured findings. What this does is *surface* what is
    already recorded, with the verification step attached.
    """
    step = Step(name="somewhere to look next")
    if not args.store.exists():
        step.note = "no store yet"
        return step

    from ...store.sqlite import SqliteStore

    store = SqliteStore(args.store)
    try:
        claims = store.attributions(address)
    finally:
        store.close()

    named = [c for c in claims if "ENS" in c.source]
    step.ran = True
    step.findings = len(named)
    if named:
        step.note = f"{len(named)} name claim(s); text records may carry handles"
        step.next_command = (
            f"chainscope label {address} --local labels.json   # and read the rationale on each"
        )
    else:
        # Said in the terms that matter: no ENS name is not "no identity", it
        # is "nothing self-published on this chain".
        step.note = "no ENS name --- nothing self-published here to follow"
    return step


def _windowed(ctx: Any, analyzer: Any, **params: Any) -> tuple[Any, str]:
    """Run an analyzer, narrowing the block range if the history is capped.

    A busy address returns more history than any provider will hand over, and
    the honest response --- refusing --- leaves a starting command with nothing
    to start from. So it retries over a recent window, which is what a person
    does by hand, and reports *which* window so the result is never read as the
    address's whole life.

    The narrowing is bounded and stated. It is not a workaround for the
    truncation guard; it is the guard being obeyed and then the question being
    asked in a form that can be answered.
    """
    try:
        return analyzer.run(ctx, **params), ""
    except Exception as exc:
        if "requested" not in str(exc) and "truncat" not in str(exc).lower():
            raise

    # Capped, and this command's job is a next move rather than a failure.
    # Picking a window here silently would produce a figure about a fortnight
    # and present it as the address's whole life, so the narrowing is handed
    # back as a command for somebody to choose the range in.
    raise TooBusy("too active for one page --- narrow it with -p start_block / -p end_block")


def _temporal(ctx: Any, address: str) -> Step:
    from ...analysis.temporal import TemporalAnalyzer

    step = Step(name="when it acts")
    try:
        result, narrowed = _windowed(ctx, TemporalAnalyzer(), address=address)
    except TooBusy as exc:
        step.note = str(exc)
        step.next_command = (
            f"chainscope analyze temporal -p address={address} "
            f"-p start_block=<N> -p end_block=<M>"
        )
        return step
    except Exception as exc:
        step.note = f"could not run: {str(exc)[:80]}"
        return step

    step.ran = True
    step.findings = len(result.findings)
    step.warnings = ([narrowed] if narrowed else []) + list(result.warnings)
    step.note = result.findings[0].title if result.findings else "no timing pattern"
    return step


def _probing(ctx: Any, address: str) -> Step:
    from ...analysis.probing import ProbingAnalyzer

    step = Step(name="tested a route first")
    try:
        result, narrowed = _windowed(ctx, ProbingAnalyzer(), address=address)
    except TooBusy as exc:
        step.note = str(exc)
        step.next_command = (
            f"chainscope analyze probing -p address={address} "
            f"-p start_block=<N> -p end_block=<M>"
        )
        return step
    except Exception as exc:
        step.note = f"could not run: {str(exc)[:80]}"
        return step

    step.ran = True
    step.findings = len(result.findings)
    if narrowed:
        step.warnings.append(narrowed)
    if result.findings:
        step.note = result.findings[0].title
        # A probe points at a destination worth tracing, and naming it is the
        # difference between a finding and a next move.
        target = result.findings[0].data.get("destination", "")
        if target:
            step.next_command = f"chainscope analyze taint -p source={address}"
    else:
        step.note = "no escalation or test payment found"
    return step
