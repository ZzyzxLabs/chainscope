"""Check that the environment can actually answer questions.

Written to be run *before* an investigation. The failure this prevents is
subtle: without an explorer-class provider, address history silently comes back
empty and every total afterwards is wrong but plausible.
"""

from __future__ import annotations

import argparse
import shutil
from typing import Any

from ...providers.base import Capability
from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="check configuration and capabilities")
    p.add_argument("--chain", "-c", default="eth")


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...core.chainid import resolve

    chain = resolve(args.chain)
    print(f"chain: {chain}")

    print("\noptional dependencies")
    for extra, module, why in [
        ("evm", "eth_utils", "EIP-55 checksums, ABI decoding"),
        ("bitcoin", "base58", "address validation"),
        ("report", "rich", "prettier terminal output"),
    ]:
        try:
            __import__(module)
            print(f"  ok      chainscope[{extra}]  -- {why}")
        except ImportError:
            print(f"  missing chainscope[{extra}]  -- {why}")

    print("\nexternal tools")
    for tool, why in [("git", "case bundle versioning")]:
        found = shutil.which(tool)
        print(f"  {'ok     ' if found else 'missing'} {tool}  -- {why}")

    print("\ncapabilities")
    print("  No providers configured in this build.")
    print(f"  Without {Capability.ADDRESS_HISTORY.name}, listing an address's")
    print("  transactions is impossible -- plain RPC cannot do it, and an empty")
    print("  result is indistinguishable from an address with no history.")
    return 0
