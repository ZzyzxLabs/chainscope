"""Check that the environment can actually answer questions.

Written to be run *before* an investigation. The failure this prevents is
subtle: without an explorer-class provider, address history silently comes back
empty and every total afterwards is wrong but plausible.

So the output is organised around **what you can and cannot ask**, not around
what happens to be installed. Green ticks next to package names tell somebody
their pip install worked. Knowing that ``ADDRESS_HISTORY`` is unreachable tells
them which of their questions will come back empty.

This command used to print "No providers configured in this build" as a
hardcoded string. It discovered nothing and said so with the confidence of a
diagnosis, to everybody, regardless of what they had --- and it is the first
command a new user runs.
"""

from __future__ import annotations

import argparse
import shutil
from importlib.metadata import entry_points
from typing import Any

from ...providers.base import Capability
from ...render.base import Renderer

__all__ = ["add_parser", "run"]

#: What each capability lets somebody ask, phrased as the question rather than
#: the mechanism.
_WHAT_IT_UNLOCKS: dict[str, str] = {
    "ADDRESS_HISTORY": "what did this address do",
    "ASSET_TRANSFERS": "what moved in and out, internal transfers included",
    "BALANCE": "what it holds now, and its nonce",
    "TRANSACTION": "look up one transaction",
    "RECEIPT": "did it succeed, and what did it log",
    "LOGS": "search events over a block range",
    "BLOCK": "block contents and timestamps",
    "ARCHIVE_STATE": "read state as of a past block",
    "TRACE": "internal calls in detail",
    "CONTRACT_SOURCE": "verified source and ABI",
    "UTXO_SET": "unspent outputs",
    "TOKEN_METADATA": "symbol and decimals for a token",
}


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="check configuration and capabilities")
    p.add_argument("--chain", "-c", default="eth")


def _discover(group: str) -> list[tuple[str, Any, str]]:
    """Entry points in ``group``, loaded.

    Import failures are reported rather than swallowed. A plugin that raises on
    import is one somebody installed and expects to work; hiding it behind an
    empty list turns a broken install into an apparently empty one.
    """
    found: list[tuple[str, Any, str]] = []
    for ep in sorted(entry_points(group=group), key=lambda e: e.name):
        try:
            found.append((ep.name, ep.load(), ""))
        except Exception as exc:
            found.append((ep.name, None, f"{type(exc).__name__}: {exc}"))
    return found


def run(args: argparse.Namespace, render: Renderer) -> int:
    from ...config import ENV_KEYS, Settings
    from ...core.chainid import resolve

    chain = resolve(args.chain)
    print(f"chain: {chain}")

    print("\noptional dependencies")
    for extra, module, why in [
        ("evm", "eth_utils", "EIP-55 checksums, ABI decoding, CREATE derivation"),
        ("bitcoin", "base58", "address validation"),
        ("analytics", "duckdb", "exact aggregates, SQL, dashboards"),
        ("agent", "mcp", "the MCP server"),
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

    settings = Settings.load()
    print("\ncredentials")
    for name in sorted(ENV_KEYS):
        var, where = ENV_KEYS[name]
        if settings.has(name):
            print(f"  ok      {name:<12} {settings.key(name).hint()}")
        else:
            print(f"  unset   {name:<12} {var} -- {where}")
    if settings.rpc:
        print(f"  ok      {'rpc':<12} {', '.join(sorted(settings.rpc))}")

    chains = _discover("chainscope.chains")
    providers = _discover("chainscope.providers")

    print("\nplugins")
    for group, items in (("chains", chains), ("providers", providers)):
        names = ", ".join(n for n, obj, _ in items if obj is not None) or "none"
        print(f"  {group:<10} {names}")
    for group, items in (("chains", chains), ("providers", providers)):
        for name, obj, error in items:
            if obj is None:
                print(f"  BROKEN  {group}:{name} -- {error}")

    # Everything above is inventory. This is what the environment can be asked.
    reachable = Capability.NONE
    for name, provider, _ in providers:
        if provider is None:
            continue
        caps = getattr(provider, "capabilities", None)
        if caps is None:
            continue
        # A provider that needs a key it has not been given is installed and
        # unusable, which is a different state from absent and worth showing as
        # one --- it is the state somebody can fix in a minute.
        # A provider that cannot serve this chain is not evidence about this
        # chain. Sui offering ADDRESS_HISTORY says nothing about whether the
        # question is answerable on Ethereum, and reporting it as reachable
        # for every chain was exactly the "wrong but plausible" answer this
        # command exists to prevent.
        serves = getattr(provider, "serves", None)
        if callable(serves) and not serves(chain):
            continue
        configured = name not in ENV_KEYS or settings.has(name)
        if configured:
            reachable |= caps
        cost = getattr(provider, "cost", None)
        suffix = f"  ({cost.name.lower()})" if cost is not None else ""
        print(f"\n  {'ok     ' if configured else 'no key '} {name}{suffix}")
        for cap in Capability:
            if cap is not Capability.NONE and caps & cap:
                name_ = str(cap.name)
                print(
                    f"      {'·' if configured else ' '} {name_:<18} "
                    f"{_WHAT_IT_UNLOCKS.get(name_, '')}"
                )

    print(f"\nwhat you can ask about {chain}")
    unreachable = [
        cap
        for cap in Capability
        if cap is not Capability.NONE
        and not (reachable & cap)
        and str(cap.name) in _WHAT_IT_UNLOCKS
    ]
    if not providers:
        print("  Nothing --- no providers are installed.")
    elif not unreachable:
        print("  Everything this build knows how to ask.")
    else:
        for cap in unreachable:
            name = str(cap.name)
            print(f"  unreachable  {name:<18} {_WHAT_IT_UNLOCKS[name]}")

    if not reachable & Capability.ADDRESS_HISTORY:
        print(
            "\n  ADDRESS_HISTORY is the one to fix first. No JSON-RPC method lists\n"
            "  an address's transactions, so without it that question has no\n"
            "  answer --- and an empty result is indistinguishable from an address\n"
            "  that never did anything. Set ETHERSCAN_API_KEY: free, 60+ chains."
        )
        # Non-zero so a setup script notices. The check is worth nothing if a
        # pipeline treats "cannot answer the central question" as success.
        return 1
    return 0
