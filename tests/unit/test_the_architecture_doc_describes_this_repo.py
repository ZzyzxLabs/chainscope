"""The package layout in ARCHITECTURE.md must be this package's layout.

A layout diagram is the map a new reader uses to find things, and it goes stale
silently --- nothing fails when a package is added. This one had: `agent/`,
`osint/` and `server/` were missing entirely (the MCP surface, the leads layer
and the extension's local server --- three of the things somebody is most likely
to be looking for), `chains/` did not mention Sui, and `analysis/` named six of
the fourteen modules that exist while listing two that live elsewhere.

A wrong map is worse than no map: it is read as authoritative, and the reader
concludes the feature is absent rather than that the document is out of date.

The check is deliberately one-directional in one place and both-directional in
the other. Every package on disk must appear; a line in the diagram must name a
real package. But `analysis/` is no longer enumerated at all --- it grows fastest,
so listing its members guarantees a wrong paragraph by the next release, and
`chainscope analyze --list` reads the registry instead.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "chainscope"
DOC = ROOT / "ARCHITECTURE.md"


def _diagram() -> str:
    text = DOC.read_text()
    start = text.index("```", text.index("## 5. Package layout"))
    return text[start : text.index("```", start + 3)]


def _listed() -> list[str]:
    return re.findall(r"[├└]── (\w+)/", _diagram())


def _packages() -> list[str]:
    return sorted(
        p.name
        for p in SOURCE.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "__init__.py").exists()
    )


class TestTheLayoutIsThisRepo:
    def test_every_package_appears(self) -> None:
        missing = sorted(set(_packages()) - set(_listed()))
        assert not missing, f"ARCHITECTURE.md does not mention {missing}"

    def test_nothing_listed_has_been_removed(self) -> None:
        gone = sorted(set(_listed()) - set(_packages()))
        assert not gone, f"ARCHITECTURE.md lists {gone}, which no longer exist"

    def test_the_diagram_is_not_empty(self) -> None:
        # Guards the parsing above: a diagram this test cannot read would make
        # both assertions above vacuously true.
        assert len(_listed()) >= 10


class TestTheChainsLineNamesTheAdaptersThatExist:
    """Sui was missing, and Sui is a chain this tool is expected to cover."""

    def _claimed(self) -> set[str]:
        line = next(ln for ln in _diagram().splitlines() if "chains/" in ln)
        inside = re.search(r"\((.*?)\)", line)
        assert inside, "the chains/ line no longer names its adapters"
        return {name.strip() for name in inside.group(1).split(",")}

    def _actual(self) -> set[str]:
        return {
            p.stem
            for p in (SOURCE / "chains").glob("*.py")
            if p.stem not in ("__init__", "base")
        }

    def test_every_adapter_is_named(self) -> None:
        assert not self._actual() - self._claimed()

    def test_none_is_named_that_does_not_exist(self) -> None:
        assert not self._claimed() - self._actual()


class TestAnalysisIsNotEnumerated:
    def test_the_line_points_at_the_registry_instead(self) -> None:
        """Listing fourteen and growing modules in prose is a promise to be
        wrong. The command reads the registry, so it cannot be."""
        line = next(ln for ln in _diagram().splitlines() if "analysis/" in ln)
        assert "analyze --list" in line

    def test_it_does_not_name_individual_modules(self) -> None:
        line = next(ln for ln in _diagram().splitlines() if "analysis/" in ln)
        modules = {
            p.stem
            for p in (SOURCE / "analysis").glob("*.py")
            if p.stem not in ("__init__", "base")
        }
        named = {m for m in modules if re.search(rf"\b{m}\b", line)}
        assert not named, f"the analysis/ line names {named}; it will go stale"
