"""A detail written as a list must render as a list.

`_wrap` called `text.split()`, which collapses every run of whitespace ---
newlines included. So an analyzer that wrote its detail as four separate points
got one run-on paragraph, the reader could not tell where one point ended and
the next began, and the author had no way to make them.

Silent, in the way this project keeps finding: a flattened paragraph still looks
like a paragraph. It surfaced only when an analyzer whose whole output is a list
of per-character reasons ("position 1 is U+0405 CYRILLIC CAPITAL LETTER DZE")
had them fused into a wall of text, and the thing that makes such a finding
checkable --- that a reader can follow it point by point --- was gone.
"""

from __future__ import annotations

from chainscope.core.result import Finding, Result, Severity
from chainscope.render.terminal import TerminalRenderer, _wrap


class TestWrap:
    def test_a_single_line_still_wraps(self) -> None:
        wrapped = _wrap("word " * 40, 30)
        assert len(wrapped) > 1
        assert all(len(line) <= 30 for line in wrapped)

    def test_newlines_are_not_collapsed(self) -> None:
        assert _wrap("first\nsecond\nthird", 72) == ["first", "second", "third"]

    def test_blank_lines_survive(self) -> None:
        # They separate a summary from the evidence under it, which is the one
        # piece of structure most worth keeping.
        assert _wrap("summary\n\ndetail", 72) == ["summary", "", "detail"]

    def test_indentation_is_kept(self) -> None:
        assert _wrap("head\n  - point", 72) == ["head", "  - point"]

    def test_a_wrapped_bullet_stays_one_bullet(self) -> None:
        # The continuation lines up under the bullet's text, not under the "-",
        # so a long point does not read as two points.
        lines = _wrap("  - " + "word " * 30, 40)
        assert lines[0].startswith("  - word")
        assert all(
            line.startswith("    ") and not line.lstrip().startswith("-") for line in lines[1:]
        )

    def test_every_line_respects_the_width(self) -> None:
        text = "short\n  - " + "verylongword " * 12 + "\n\n  - another " + "word " * 20
        assert all(len(line) <= 60 for line in _wrap(text, 60))

    def test_a_word_longer_than_the_width_is_not_lost(self) -> None:
        # A contract address is 42 characters and will not fit a narrow column.
        # Overflowing the line is right; dropping it is not.
        address = "0x" + "a" * 40
        assert address in " ".join(_wrap(f"- {address}", 20))


class TestThroughTheRenderer:
    def _rendered(self, detail: str) -> str:
        result = Result(
            analyzer="test",
            findings=(Finding(title="t", severity=Severity.NOTABLE, detail=detail),),
        )
        return TerminalRenderer(colour=False).render(result)

    def test_four_points_render_as_four_lines(self) -> None:
        detail = "\n".join(f"  - point {n}" for n in range(1, 5))
        body = [ln for ln in self._rendered(detail).splitlines() if "point" in ln]
        assert len(body) == 4

    def test_they_were_previously_one(self) -> None:
        # The old behaviour, stated so the regression is unmistakable: joining
        # on whitespace put every point on one line.
        detail = "\n".join(f"  - point {n}" for n in range(1, 5))
        assert "point 1 - point 2" not in self._rendered(detail)
