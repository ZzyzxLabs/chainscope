"""A source that could not be read must not read as "nothing known".

The single failure mode this codebase is most able to cause. Every attribution
source can fail --- a missing data file, a corrupt download, a chain the source
refuses to answer for --- and each failure is swallowed per-source so that one
bad file does not blank the panel. That is right. What is not right is the
swallowed failure never reaching the screen, because the empty claim list it
leaves behind is indistinguishable from the empty claim list an address nobody
has ever labelled produces, and the panel phrases that one as reassurance.

Found by reading a docstring that said the caller reported `reliable`. It did
not; `_claims` discarded `Resolution.failed` and no field in the response
carried it. Fifth instance of prose describing a property the code lacked.
"""

from __future__ import annotations

from pathlib import Path

from chainscope.server.local import _note


def test_a_failure_is_never_phrased_as_an_absence() -> None:
    """The two states get different sentences, and only one is reassuring."""
    quiet = _note(found=False, unreachable=[])
    broken = _note(found=False, unreachable=["darklist: file missing"])
    assert quiet != broken
    assert "not a clean result" in broken
    assert "darklist: file missing" in broken
    # The reassuring phrasing belongs only to the state that earned it.
    assert "every configured source answered" in " ".join(quiet.split())
    assert "every configured source" not in broken


def test_a_partial_answer_says_it_is_partial() -> None:
    """Claims found *and* a source down is still incomplete."""
    note = _note(found=True, unreachable=["ethlabels: unreadable"])
    assert "incomplete" in note.lower()
    assert "ethlabels: unreadable" in note
    # A complete answer says nothing; silence must mean everything was asked.
    assert _note(found=True, unreachable=[]) == ""


def test_the_page_renders_the_distinction() -> None:
    """It must reach the screen, not just the JSON."""
    source = Path("src/chainscope/server/webapp.py").read_text()
    assert "found.reliable === false" in source
    assert "unreachable_sources" in source
    assert "An empty result here is not a clean one." in source


def test_the_response_carries_both_fields() -> None:
    """`resolve` states reliability rather than leaving it inferred."""
    handler = Path("src/chainscope/server/local.py").read_text()
    body = handler[handler.index("def resolve") :]
    body = body[: body.index("def _from_sources")]
    assert '"unreachable_sources": unreachable' in body
    assert '"reliable": not unreachable' in body
