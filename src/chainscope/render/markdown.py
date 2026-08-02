"""Markdown reports.

Written for the document that outlives the investigation. Two sections exist
solely so a reader months later can judge the work rather than merely read its
conclusions: the caveats block, and the reproducibility block naming the exact
parameters used.
"""

from __future__ import annotations

from ..core.result import Finding, Result, Severity
from .base import Renderer

__all__ = ["MarkdownRenderer"]

_ICON = {
    Severity.INFO: "·",
    Severity.NOTABLE: "▪",
    Severity.IMPORTANT: "▲",
    Severity.CRITICAL: "⛔",
}


class MarkdownRenderer(Renderer):
    name = "markdown"

    def __init__(self, *, heading_level: int = 2, include_data: bool = True) -> None:
        self.h = heading_level
        self.include_data = include_data

    def render(self, result: Result) -> str:
        h = "#" * self.h
        out = [f"{h} {result.analyzer}\n"]

        if result.warnings:
            out.append(
                "> **Read these first.** They change how the findings "
                "below should be interpreted.\n>"
            )
            for w in result.warnings:
                out.append(f"> - {w}")
            out.append("")

        if result.is_empty:
            out.append("_No findings._\n")
        else:
            for f in result.findings:
                out.extend(self._finding(f))

        if result.hypotheses:
            out.append(f"{h}# Hypotheses\n")
            out.append(
                "These are inferences with scores, not observations. "
                "The factor breakdown is shown so the reasoning can be "
                "checked rather than taken on trust.\n"
            )
            for i, hyp in enumerate(result.hypotheses, 1):
                out.append(f"**{i}. {hyp.claim}** — score {hyp.score:g}\n")
                out.append("| Factor | Contribution | Note |")
                out.append("|---|---|---|")
                for factor in hyp.factors:
                    out.append(
                        f"| `{factor.name}` | {factor.contribution:+g} | {factor.note} |"
                    )
                if hyp.is_contested and hyp.alternatives:
                    out.append(
                        f"\n> ⚠️ Contested: the next candidate scores "
                        f"{hyp.alternatives[0].score:g}. This ranking is not "
                        f"decisive and should not be reported as a conclusion.\n"
                    )
                out.append("")

        out.extend(self._reproducibility(result))
        return "\n".join(out)

    def _finding(self, f: Finding) -> list[str]:
        h = "#" * (self.h + 1)
        out = [f"{h} {_ICON[f.severity]} {f.title}\n"]
        if f.detail:
            out.append(f"{f.detail}\n")
        if self.include_data and f.data:
            rows = [
                f"| `{k}` | {_cell(v)} |"
                for k, v in f.data.items()
                if v not in (None, [], {}, "")
            ]
            if rows:
                out.append("| Field | Value |")
                out.append("|---|---|")
                out.extend(rows)
                out.append("")
        return out

    def _reproducibility(self, result: Result) -> list[str]:
        h = "#" * (self.h + 1)
        out = [f"{h} Reproducibility\n"]
        if result.params:
            out.append("Parameters used:\n")
            out.append("```json")
            import json

            out.append(json.dumps(result.params, indent=2, default=str))
            out.append("```\n")
        if result.evidence.query_keys:
            out.append(
                f"{len(result.evidence.query_keys)} queries recorded. With the "
                f"case bundle, this analysis can be replayed offline.\n"
            )
        if result.finished_at:
            out.append(
                f"_Run {result.finished_at:%Y-%m-%d %H:%M:%S} UTC "
                f"by {result.analyzer} v{result.version}._"
            )
        return out


def _cell(value: object) -> str:
    if isinstance(value, list):
        if len(value) <= 3:
            return ", ".join(f"`{v}`" for v in value)
        return f"{len(value)} items (first: `{value[0]}`)"
    if isinstance(value, str) and len(value) > 60:
        return f"`{value[:57]}…`"
    return f"`{value}`"
