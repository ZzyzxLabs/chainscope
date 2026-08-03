"""Fail if the Python and TypeScript descriptions of the views disagree.

There are two copies: `chainscope.server.site.VIEWS`, served by the Python
process that holds the case, and `web/src/lib/views.ts`, built into the static
marketing and documentation pages. Both describe the same eight views.

Two descriptions of one tool drift, and the drift always favours the one nobody
reads --- which here is whichever surface the author was not looking at when
they changed something. The `cannot` fields are the ones that matter: they are
the difference between a reader who knows a truncated graph is possible and one
who does not, and they are exactly the sort of prose that gets improved in one
place and left in the other.

Run by CI and by `make check`. Compares the *content*, not the formatting: the
TypeScript is prettier-wrapped and the Python is black-wrapped, so both sides
are normalised to single-spaced text before comparison.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS = ROOT / "web" / "src" / "lib" / "views.ts"

#: Field names differ between the two --- `use`/`for` and `cannot`/`limits` ---
#: because `for` is a Python keyword and `use` reads better in JSX. Mapped here
#: rather than renamed on either side, since both names are right in context.
FIELDS = (("name", "name"), ("what", "what"), ("use", "for"), ("cannot", "limits"))


def _normalise(text: str) -> str:
    """Collapse whitespace so wrapping differences are not reported as drift."""
    return " ".join(text.split())


def _typescript_views(source: str) -> list[dict[str, str]]:
    """Parse the `VIEWS` array without a JS engine.

    A regex over a hand-maintained literal, which is fragile in general --- but
    the alternative is requiring node to run a lint check, and this file's whole
    purpose is to run everywhere CI does. If the shape ever defeats it, the
    parse returns nothing and the caller fails loudly rather than passing.
    """
    block = re.search(r"export const VIEWS: View\[\] = \[(.*?)\n\];", source, re.S)
    if not block:
        raise SystemExit("check-views-match: could not find the VIEWS array in views.ts")

    views: list[dict[str, str]] = []
    for entry in re.finditer(r"\{(.*?)\n  \}", block.group(1), re.S):
        body = entry.group(1)
        view: dict[str, str] = {}
        for field in ("name", "where", "what", "use", "cannot"):
            # A field is either one string or several concatenated with `+`.
            found = re.search(rf'\b{field}:\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+),', body, re.S)
            if not found:
                continue
            parts = re.findall(r'"((?:[^"\\]|\\.)*)"', found.group(1))
            view[field] = _normalise("".join(parts).replace('\\"', '"'))
        if view:
            views.append(view)
    return views


def _python_views() -> list[dict[str, str]]:
    """Read `VIEWS` out of the Python source without importing the package.

    `ast.literal_eval` on the assignment, so this check does not depend on the
    package being installed --- CI runs it before the editable install in at
    least one job, and a check that only runs sometimes is not a check.
    """
    path = ROOT / "src" / "chainscope" / "server" / "site.py"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "VIEWS":
            raw = ast.literal_eval(node.value) if node.value else ()
            return [{k: _normalise(str(v)) for k, v in view.items()} for view in raw]
    raise SystemExit("check-views-match: could not find VIEWS in site.py")


def main() -> int:
    python_views = _python_views()
    ts_views = _typescript_views(TS.read_text())

    problems: list[str] = []
    if len(python_views) != len(ts_views):
        problems.append(
            f"different numbers of views: site.py has {len(python_views)}, "
            f"views.ts has {len(ts_views)}"
        )

    by_name = {v["name"]: v for v in ts_views}
    for view in python_views:
        name = view["name"]
        other = by_name.get(name)
        if other is None:
            problems.append(f"{name!r} is in site.py but not in views.ts")
            continue
        for ts_field, py_field in FIELDS:
            mine, theirs = view.get(py_field, ""), other.get(ts_field, "")
            if mine != theirs:
                problems.append(
                    f"{name!r} field {py_field!r}/{ts_field!r} differs:\n"
                    f"    site.py:  {mine}\n"
                    f"    views.ts: {theirs}"
                )

    for name in set(by_name) - {v["name"] for v in python_views}:
        problems.append(f"{name!r} is in views.ts but not in site.py")

    if problems:
        print("The two descriptions of the views have drifted apart.\n")
        print(
            "Both are read by somebody: site.py answers the local server, "
            "views.ts builds the public docs. A view described one way in one "
            "and another way in the other means one set of readers is being "
            "told something untrue.\n"
        )
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"views match: {len(python_views)} views, {len(FIELDS)} fields each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
