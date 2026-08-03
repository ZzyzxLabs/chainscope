"""A method document's header must be true of the module it names.

Every file in `docs/methods/` opens with a claim about what the technique
produces and which code implements it. Those headers are the first thing a
reader checks before quoting a result, and all four were wrong in the same way:
each named a `Method` --- `HEURISTIC`, `INFERENCE` --- that appeared nowhere in
the module it pointed at. `Method` describes how an `Attribution` was arrived
at, and none of these four analyzers writes an `Attribution`. They emit findings
and hypotheses.

`consolidation.md` was wrong twice over. It promised the analyzer "never asserts
attribution above `MEDIUM`", and the analyzer asserted none at all --- while
emitting a field called `confidence` that held the *hub's label* confidence, so
a consumer reading `{"fan_in": 12, "confidence": "CERTAIN"}` had every reason to
read certainty about the clustering. `clustering.md` claimed `MEDIUM` from a
module that names no confidence anywhere.

Documentation drifts because nothing fails when it does. This reads the headers
and checks them, so now something does.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "methods"

#: Doc stem -> the dotted module its "Implemented by" line points into.
#: Derived from the documents themselves in `test_every_doc_is_covered`, so a
#: new method document cannot quietly skip this file.
EXPECTED = {
    "impersonation": "chainscope.analysis.impersonation",
    "address-poisoning": "chainscope.analysis.poisoning",
    "routing": "chainscope.analysis.route",
    "contributors": "chainscope.analysis.contributors",
    "consolidation": "chainscope.analysis.consolidation",
    "change-detection": "chainscope.analysis.peel",
    "clustering": "chainscope.analysis.cluster",
    "cross-chain": "chainscope.analysis.xchain",
}


def _docs() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


def _implemented_by(text: str) -> str:
    match = re.search(r"\*\*Implemented by:\*\*\s*`([^`]+)`", text)
    assert match, "no 'Implemented by' line"
    dotted = match.group(1)
    # Trim the trailing symbol: the line names a function or a class inside a
    # module, and only the module can be imported.
    parts = dotted.split(".")
    while parts:
        try:
            importlib.import_module(".".join(parts))
            return ".".join(parts)
        except ImportError:
            parts.pop()
    raise AssertionError(f"nothing importable in {dotted!r}")


def _claims(text: str) -> str:
    """The bolded header lines --- what a reader takes as fact about the code.

    Not the whole header block: a document may need to quote a claim in order
    to say it was wrong, and `consolidation.md` does.
    """
    return "\n".join(ln for ln in text.splitlines() if ln.startswith("**"))


def _names(module_name: str, kind: str) -> set[str]:
    module = importlib.import_module(module_name)
    return set(re.findall(rf"{kind}\.(\w+)", inspect.getsource(module)))


class TestEveryDocumentIsChecked:
    def test_there_is_at_least_one(self) -> None:
        assert _docs()

    def test_the_set_has_not_changed_silently(self) -> None:
        # A new method document must be added to EXPECTED, which is the prompt
        # to check its header against its module.
        assert {p.stem for p in _docs()} == set(EXPECTED)


@pytest.mark.parametrize("doc", _docs(), ids=lambda p: p.stem)
class TestTheHeaderIsTrue:
    def test_the_implementation_it_names_exists(self, doc: Path) -> None:
        assert _implemented_by(doc.read_text()) == EXPECTED[doc.stem]

    def test_it_does_not_claim_a_method_the_module_never_uses(self, doc: Path) -> None:
        """The original defect, in all four.

        Only the **bolded claim lines** are read --- the ones a reader takes as
        fact about the code. Prose may name `Method` freely, and it has to:
        `consolidation.md` explains the wrong claim by quoting it.
        """
        claimed = set(re.findall(r"`?Method\.(\w+)`?", _claims(doc.read_text())))
        actual = _names(EXPECTED[doc.stem], "Method")
        assert not claimed - actual, (
            f"{doc.name} claims Method.{claimed - actual}; the module uses {actual or 'none'}"
        )

    def test_every_confidence_it_claims_is_one_the_module_names(self, doc: Path) -> None:
        text = doc.read_text()
        line = next(
            (ln for ln in text.splitlines() if ln.startswith("**Confidence produced:**")),
            None,
        )
        assert line is not None, f"{doc.name} has no 'Confidence produced' line"
        claimed = set(re.findall(r"`(SPECULATIVE|LOW|MEDIUM|HIGH|CERTAIN)`", line))
        actual = _names(EXPECTED[doc.stem], "Confidence")
        assert claimed <= actual, (
            f"{doc.name} claims {claimed - actual}, module names {actual or 'none'}"
        )

    def test_a_module_that_states_no_confidence_does_not_promise_one(self, doc: Path) -> None:
        # The `clustering.md` case: a MEDIUM ceiling promised by a module with
        # no `Confidence` in it anywhere.
        text = doc.read_text()
        line = next(ln for ln in text.splitlines() if ln.startswith("**Confidence produced:**"))
        if _names(EXPECTED[doc.stem], "Confidence"):
            return
        assert "none" in line.lower(), (
            f"{doc.name} promises a confidence its module never states"
        )
