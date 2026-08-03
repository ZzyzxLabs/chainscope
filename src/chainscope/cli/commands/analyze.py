"""Run analyzers and render their results."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points
from typing import Any

from ...analysis.base import Analyzer, Context
from ...providers.build import router_for
from ...render.base import Renderer

__all__ = ["add_parser", "available", "rejected", "run"]


def _discover() -> tuple[dict[str, type[Analyzer]], dict[str, str]]:
    """Load registered entry points, separating the usable from the broken.

    An entry point can resolve to anything --- a module, a function, a class
    that is not an ``Analyzer`` --- and two of this package's own once did. The
    contract is checked *here*, at discovery, so that a misregistration is
    reported as itself rather than deferred to ``cls()`` and surfacing as
    "needs constructor arguments (a data source)", which is a different problem
    and sends the reader looking in the wrong place.

    Rejects are returned rather than dropped. A plugin that fails to import is
    the case where silence is most expensive: the user installed it, ``--list``
    does not mention it, and nothing says why.
    """
    found: dict[str, type[Analyzer]] = {}
    broken: dict[str, str] = {}
    for ep in entry_points(group="chainscope.analyzers"):
        try:
            obj = ep.load()
        except Exception as exc:
            broken[ep.name] = f"failed to import ({type(exc).__name__}: {exc})"
            _BROKEN_SOURCE[ep.name] = ep.value
            continue
        if not (isinstance(obj, type) and issubclass(obj, Analyzer)):
            kind = "function" if callable(obj) else type(obj).__name__
            broken[ep.name] = (
                f"{ep.value} is a {kind}, not an Analyzer subclass; "
                f"the entry point points at the wrong object"
            )
            _BROKEN_SOURCE[ep.name] = ep.value
            continue
        found[ep.name] = obj
    return found, broken


#: Where a broken registration came from, recorded at discovery so the exit
#: code can tell this package's packaging mistake from a plugin author's.
_BROKEN_SOURCE: dict[str, str] = {}


def _is_ours(name: str, broken: dict[str, str]) -> bool:
    return _BROKEN_SOURCE.get(name, "").startswith("chainscope.")


def available() -> dict[str, type[Analyzer]]:
    """Analyzers registered by this package or any installed plugin."""
    return _discover()[0]


def rejected() -> dict[str, str]:
    """Registered names that did not satisfy the ``Analyzer`` contract."""
    return _discover()[1]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="run an analysis")
    p.add_argument("analyzer", nargs="?", help="analyzer name; omit with --list")
    p.add_argument("--list", action="store_true", help="show registered analyzers")
    p.add_argument("--chain", "-c", default="eth")
    p.add_argument(
        "--single-source",
        action="store_true",
        help="ask one provider per enumeration instead of two. Faster and "
        "cheaper; a short answer then has nothing to disagree with it. The "
        "result says which it was either way",
    )
    p.add_argument(
        "--param",
        "-p",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="analyzer parameter; repeatable",
    )
    p.add_argument("--max-nodes", type=int)
    p.add_argument("--max-depth", type=int)


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--param expects KEY=VALUE, got {pair!r}")
        key, _, value = pair.partition("=")
        if value.isdigit():
            out[key] = int(value)
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...core.chainid import resolve

    registry, broken = _discover()

    if args.list or not args.analyzer:
        if not registry and not broken:
            print("no analyzers registered")
            return 1
        if registry:
            print("registered analyzers\n")
            for name, cls in sorted(registry.items()):
                instance = _safe_instantiate(cls)
                desc = getattr(instance, "description", "") or cls.__doc__ or ""
                print(f"  {name:20} {desc.strip().splitlines()[0] if desc else ''}")
        if broken:
            print("\nregistered but unusable\n")
            for name, why in sorted(broken.items()):
                print(f"  {name:20} {why}")
        # Non-zero for *our own* broken registrations, which is what this was
        # written for: two of this package's entry points once pointed at plain
        # functions, and exiting zero would let that ship again.
        #
        # A third-party plugin's mistake is reported above and does not fail the
        # command. Otherwise installing somebody's broken plugin makes every
        # `chainscope analyze --list` in a user's scripts exit 1 forever, for a
        # defect in a package they cannot fix from here --- and the question
        # `--list` asks, "what is available", was answered.
        return 1 if any(_is_ours(name, broken) for name in broken) else 0

    if args.analyzer in broken:
        print(f"{args.analyzer} is registered but unusable: {broken[args.analyzer]}")
        return 2

    if args.analyzer not in registry:
        print(f"unknown analyzer {args.analyzer!r}; try --list")
        return 2

    instance = _safe_instantiate(registry[args.analyzer])
    if instance is None:
        print(
            f"{args.analyzer} needs constructor arguments (a data source) and "
            f"cannot be run from the CLI yet; use the Python API"
        )
        return 2

    limits = {}
    if args.max_nodes:
        limits["max_nodes"] = args.max_nodes
    if args.max_depth:
        limits["max_depth"] = args.max_depth

    chain = resolve(args.chain)
    router, skipped = router_for(chain, corroborate=not args.single_source)
    ctx = Context(chain=chain, router=router, limits=limits)
    if not instance.applicable(ctx):
        # Say which providers were considered and what each one needs. The
        # previous message named none of that and pointed at `doctor`, which
        # answered a different question -- it reads entry points, so it listed
        # capabilities as available while this router held nothing at all.
        print(
            f"{args.analyzer} cannot run on {chain}: none of the available "
            f"providers offer the capabilities it needs."
        )
        if skipped:
            print()
            for name, why in sorted(skipped.items()):
                print(f"  {name:12} {why}")
        elif not router.providers:
            print("\n  no providers are installed for this chain")
        return 2

    try:
        params = _parse_params(args.param)
    except ValueError as exc:
        # 2, matching "unknown analyzer" above. Both are the caller getting the
        # arguments wrong, and a script should be able to tell that from an
        # analysis that ran and failed. It reached `main`'s catch-all and came
        # back as 1, which conflates the two.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # One analyzer here, but scoped anyway: the audit log may already hold
    # queries from resolver warm-up or an earlier command sharing the process,
    # and evidence that reaches backwards past its own analyzer is the defect
    # regardless of how it got there.
    with ctx.scope() as scoped:
        result = instance.run(scoped, **params)
    print(render.render(result))
    # Warnings mean the result is qualified. Exiting zero would let a pipeline
    # treat a truncated search as a complete one.
    return 1 if result.warnings else 0


def _safe_instantiate(cls: type[Analyzer]) -> Analyzer | None:
    try:
        return cls()
    except TypeError:
        return None
