"""Run analyzers and render their results."""

from __future__ import annotations

import argparse
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
            continue
        if not (isinstance(obj, type) and issubclass(obj, Analyzer)):
            kind = "function" if callable(obj) else type(obj).__name__
            broken[ep.name] = (
                f"{ep.value} is a {kind}, not an Analyzer subclass; "
                f"the entry point points at the wrong object"
            )
            continue
        found[ep.name] = obj
    return found, broken


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
        # A broken registration is a real defect in whatever registered it, and
        # exiting zero would let a packaging mistake ship unnoticed.
        return 1 if broken else 0

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
    router, skipped = router_for(chain)
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

    result = instance.run(ctx, **_parse_params(args.param))
    print(render.render(result))
    # Warnings mean the result is qualified. Exiting zero would let a pipeline
    # treat a truncated search as a complete one.
    return 1 if result.warnings else 0


def _safe_instantiate(cls: type[Analyzer]) -> Analyzer | None:
    try:
        return cls()
    except TypeError:
        return None
