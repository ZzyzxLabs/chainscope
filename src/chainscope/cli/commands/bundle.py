"""Inspect and verify case bundles."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ...case.bundle import Bundle, BundleError
from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="inspect a case bundle")
    p.add_argument("path", type=Path)
    p.add_argument("--archive", type=Path, help="zip the bundle to this path")


def run(args: argparse.Namespace, render: Renderer) -> int:
    try:
        b = Bundle.open(args.path)
    except BundleError as exc:
        print(f"error: {exc}")
        return 2

    s = b.summary()
    print(f"{s['title'] or '(untitled)'}")
    print(f"  subject     {s['subject'] or '-'}")
    print(f"  created     {s['created_at']}")
    print(f"  tool        chainscope {s['tool_version']}")
    print(f"  analyses    {s['analyses']}")
    print(f"  findings    {s['total_findings']}")
    print(f"  warnings    {s['total_warnings']}")

    if s["replayable"]:
        print("  replayable  yes -- queries are bundled; this can be rerun offline")
    else:
        print("  replayable  NO -- no query cache attached, so the findings")
        print("              here cannot be independently verified")

    if b.notes:
        print(f"\nnotes\n  {b.notes}")

    print("\nanalyses")
    for i, entry in enumerate(b.results):
        flag = f"  ({entry.get('warnings')} warning(s))" if entry.get("warnings") else ""
        print(f"  {i}. {entry.get('analyzer')}  {entry.get('findings')} finding(s){flag}")

    if args.archive:
        dest = b.archive(args.archive)
        print(f"\narchived to {dest}")

    return 0 if s["replayable"] else 1
