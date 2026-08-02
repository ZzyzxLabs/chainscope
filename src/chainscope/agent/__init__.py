"""Agent-facing surfaces.

Kept in its own package because the dependency is optional and the concerns are
different: everything else here answers questions for a program, and this
answers them for something that will paraphrase the answer.
"""

from __future__ import annotations

__all__ = ["AgentError", "ServerConfig", "build_server"]


def __getattr__(name: str) -> object:
    # Lazy, so importing chainscope does not require the MCP SDK.
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(name)
