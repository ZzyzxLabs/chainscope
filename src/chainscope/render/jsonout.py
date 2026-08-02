"""Machine-readable output.

For piping into other tools. Confidence and warnings are included at the top
level rather than buried, so a consumer cannot accidentally read findings
without the caveats attached to them.
"""

from __future__ import annotations

import json

from ..core.result import Result
from .base import Renderer

__all__ = ["JsonRenderer"]


class JsonRenderer(Renderer):
    name = "json"

    def __init__(self, *, indent: int | None = 2) -> None:
        self.indent = indent

    def render(self, result: Result) -> str:
        payload = result.to_dict()
        # Surfaced deliberately: a consumer that reads `findings` and ignores
        # `warnings` will report a truncated search as a complete one.
        payload["reliable"] = not result.warnings
        return json.dumps(payload, indent=self.indent, default=str)

    def render_all(self, results: list[Result]) -> str:
        return json.dumps(
            {
                "results": [r.to_dict() for r in results],
                "reliable": not any(r.warnings for r in results),
            },
            indent=self.indent,
            default=str,
        )
