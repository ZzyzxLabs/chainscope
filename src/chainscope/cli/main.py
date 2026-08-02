"""Command-line entry point.

Deliberately thin: parse, dispatch, render, exit. Every command module returns
``Result`` objects and lets the renderer decide how they look, so `--format
json` is free rather than a parallel code path that drifts.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ..render.base import Renderer
from ..render.jsonout import JsonRenderer
from ..render.markdown import MarkdownRenderer
from ..render.terminal import TerminalRenderer
from .commands import analyze, bundle, doctor, label, tag

__all__ = ["main"]

_COMMANDS = {
    "analyze": analyze,
    "label": label,
    "tag": tag,
    "doctor": doctor,
    "bundle": bundle,
}


def renderer_for(name: str, verbose: bool) -> Renderer:
    if name == "json":
        return JsonRenderer()
    if name == "markdown":
        return MarkdownRenderer()
    return TerminalRenderer(verbose=verbose)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chainscope",
        description=(
            "Open-source blockchain forensics. Heuristic output is a lead, "
            "not evidence --- see the confidence level on every claim."
        ),
    )
    p.add_argument(
        "--format", "-f", choices=["terminal", "json", "markdown"], default="terminal"
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--no-colour", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)
    for name, module in _COMMANDS.items():
        module.add_parser(sub, name)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    module = _COMMANDS[args.command]
    render = renderer_for(args.format, args.verbose)
    if args.no_colour and isinstance(render, TerminalRenderer):
        render.colour = False
    try:
        return int(module.run(args, render))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        print("run with --verbose for a traceback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
