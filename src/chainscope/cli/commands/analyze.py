"""Run analyzers and render their results."""

from __future__ import annotations

import argparse
from importlib.metadata import entry_points
from typing import Any

from ...analysis.base import Analyzer, Context
from ...providers.router import Router
from ...render.base import Renderer

__all__ = ["add_parser", "available", "run"]


def available() -> dict[str, type[Analyzer]]:
    """Analyzers registered by this package or any installed plugin."""
    found: dict[str, type[Analyzer]] = {}
    for ep in entry_points(group="chainscope.analyzers"):
        try:
            found[ep.name] = ep.load()
        except Exception:
            continue
    return found


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

    registry = available()

    if args.list or not args.analyzer:
        if not registry:
            print("no analyzers registered")
            return 1
        print("registered analyzers\n")
        for name, cls in sorted(registry.items()):
            instance = _safe_instantiate(cls)
            desc = getattr(instance, "description", "") or cls.__doc__ or ""
            print(f"  {name:20} {desc.strip().splitlines()[0] if desc else ''}")
        return 0

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

    ctx = Context(chain=resolve(args.chain), router=Router(), limits=limits)
    if not instance.applicable(ctx):
        print(
            f"{args.analyzer} cannot run here: the configured providers do not "
            f"offer the capabilities it needs. Run `chainscope doctor`."
        )
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
