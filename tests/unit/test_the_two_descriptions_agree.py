"""The Python and TypeScript descriptions of the views must not drift.

`site.py` answers the local server; `web/src/lib/views.ts` builds the public
documentation. Both describe the same eight views, and the `cannot` fields --- the
ones that tell a reader a truncated graph is possible --- are exactly the prose
that gets improved in one place and left in the other.

This is not hypothetical. The check was written and immediately found four
views whose wording had diverged, all within the session that created both
copies. A guard that is only run by hand is a guard that runs once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_views_match() -> None:
    """Runs the same script CI does, so there is one implementation of the rule."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_views_match.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_checker_actually_detects_drift(tmp_path: Path) -> None:
    """A checker that cannot fail is decoration.

    Feeds it a deliberately altered copy and requires a non-zero exit. Without
    this, a parser that silently found no views would pass forever.
    """
    import re

    source = (ROOT / "web" / "src" / "lib" / "views.ts").read_text()
    broken = source.replace("hop distance, not time", "hop distance and also time", 1)
    assert broken != source, "the fixture text moved; update this test"

    fake_root = tmp_path / "web" / "src" / "lib"
    fake_root.mkdir(parents=True)
    (fake_root / "views.ts").write_text(broken)

    script = (ROOT / "scripts" / "check_views_match.py").read_text()
    # Point the copy at the altered views.ts while keeping the real site.py.
    script = script.replace(
        'TS = ROOT / "web" / "src" / "lib" / "views.ts"',
        f'TS = Path({str(tmp_path)!r}) / "web" / "src" / "lib" / "views.ts"',
    )
    script = re.sub(r"^ROOT = .*$", f"ROOT = Path({str(ROOT)!r})", script, count=1, flags=re.M)
    probe = tmp_path / "probe.py"
    probe.write_text(script)

    result = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode != 0, "drift was introduced and the checker passed anyway"
    assert "drifted apart" in result.stdout
