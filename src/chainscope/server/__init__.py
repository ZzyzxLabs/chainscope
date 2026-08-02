"""Local HTTP surface.

Separate from the MCP server because the audience differs: that one speaks
stdio to a single agent, this one answers a browser while somebody reads a page.
"""

from __future__ import annotations

__all__ = ["LocalServer", "ServerOptions"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from . import local

        return getattr(local, name)
    raise AttributeError(name)
