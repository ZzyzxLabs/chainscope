"""Terminal output.

Optimised for the reading order of someone mid-investigation: warnings first,
because they change how everything below should be read.
"""

from __future__ import annotations

import os
import sys

from ..core.hypothesis import Hypothesis
from ..core.result import Finding, Result, Severity
from .base import Renderer

__all__ = ["TerminalRenderer"]

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_SEVERITY_COLOUR = {
    Severity.INFO: "\033[36m",
    Severity.NOTABLE: "\033[33m",
    Severity.IMPORTANT: "\033[35m",
    Severity.CRITICAL: "\033[31m",
}


class TerminalRenderer(Renderer):
    name = "terminal"

    def __init__(self, *, colour: bool | None = None, verbose: bool = False) -> None:
        if colour is None:
            # NO_COLOR is a widely honoured convention; piping to a file should
            # not embed escape codes either.
            colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.colour = colour
        self.verbose = verbose

    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.colour else text

    def render(self, result: Result) -> str:
        out: list[str] = [
            self._c(f"{result.analyzer} v{result.version}", _BOLD),
            "─" * min(len(result.analyzer) + 20, 76),
        ]

        # Warnings first. They govern how everything below should be read, and
        # a reader who meets them last has already formed a conclusion.
        for w in result.warnings:
            out.append(self._c(f"  ! {w}", _SEVERITY_COLOUR[Severity.NOTABLE]))
        if result.warnings:
            out.append("")

        if result.is_empty:
            out.append(self._c("  (no findings)", _DIM))
            return "\n".join(out)

        for f in result.findings:
            out.extend(self._finding(f))

        if result.hypotheses:
            out.append("")
            out.append(self._c("  Hypotheses (inference, not observation)", _BOLD))
            for i, h in enumerate(result.hypotheses, 1):
                out.extend(self._hypothesis(h, i))

        if result.duration is not None:
            out.append(self._c(f"\n  {result.duration:.2f}s", _DIM))
        return "\n".join(out)

    def _finding(self, f: Finding) -> list[str]:
        colour = _SEVERITY_COLOUR[f.severity]
        lines = [f"\n  {self._c('▪', colour)} {self._c(f.title, _BOLD)}"]
        if f.detail:
            for chunk in _wrap(f.detail, 72):
                lines.append(f"    {chunk}")
        if self.verbose and f.data:
            for k, v in f.data.items():
                if v in (None, [], {}, ""):
                    continue
                rendered = v if not isinstance(v, list) else f"{len(v)} item(s)"
                lines.append(self._c(f"    {k}: {rendered}", _DIM))
        return lines

    def _hypothesis(self, h: Hypothesis, index: int) -> list[str]:
        mark = "★" if index == 1 else " "
        lines = [f"\n  {mark} {h.claim}  {self._c(f'[score {h.score:g}]', _DIM)}"]
        for factor in h.factors:
            if factor.contribution == 0 and not self.verbose:
                continue
            lines.append(self._c(f"      {factor}", _DIM))
        if h.is_contested and h.alternatives:
            runner_up = h.alternatives[0]
            lines.append(
                self._c(
                    f"      ! contested: next scores {runner_up.score:g}; "
                    f"this ranking is not decisive",
                    _SEVERITY_COLOUR[Severity.IMPORTANT],
                )
            )
        return lines


def _wrap(text: str, width: int) -> list[str]:
    """Wrap to ``width``, keeping the line structure the caller wrote.

    ``text.split()`` collapses every run of whitespace, newlines included, so a
    detail written as a list of points came out as one run-on paragraph --- the
    reader could not tell where one point ended and the next began, and the
    author had no way to make them. Silent, because a flattened paragraph still
    looks like a paragraph.

    Blank lines are kept as blank lines: they separate a summary from the
    evidence under it, which is the one piece of structure most worth having.
    Leading indentation is carried onto the continuation lines of the same
    point, so a wrapped bullet stays visibly one bullet.
    """
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            out.append("")
            continue
        stripped = line.lstrip()
        indent = " " * (len(line) - len(stripped))
        # Continuations are indented past the marker so the bullet's text lines
        # up under itself rather than under the "-".
        hanging = indent + ("  " if stripped[:2] in ("- ", "* ") else "")
        current = indent
        for word in stripped.split():
            candidate = f"{current}{word}" if not current.strip() else f"{current} {word}"
            if len(candidate) > width and current.strip():
                out.append(current)
                current = f"{hanging}{word}"
            else:
                current = candidate
        if current.strip():
            out.append(current)
    return out
