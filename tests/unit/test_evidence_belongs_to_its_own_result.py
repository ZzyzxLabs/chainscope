"""A result's evidence must be its own queries, not the session's.

`Result.evidence` says it is "the queries supporting this result". It was every
cache key the audit log had seen, because `query_keys()` read the whole buffer
and one `Context` is shared across every analyzer in an investigation. So a
peel-chain result cited the requests that produced an unrelated cross-chain
one, and `attest` --- which hashes those responses --- proved the bytes were
unmodified while proving nothing about which response supported which finding.

That is the provenance thesis inverted: the artefact that exists to link a claim
to its source instead asserts a link that was never checked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from chainscope.analysis.base import Context
from chainscope.core.chainid import ETHEREUM
from chainscope.transport.audit import AuditLog, AuditRecord


def _log_query(audit: AuditLog, key: str) -> None:
    audit.record(
        AuditRecord(
            timestamp=datetime.now(timezone.utc),
            kind="http.get",
            url="https://example.invalid",
            provider="test",
            cache_key=key,
        )
    )


def _ctx(audit: AuditLog) -> Context:
    return Context(chain=ETHEREUM, router=None, audit=audit)  # type: ignore[arg-type]


def test_a_scope_excludes_what_happened_before_it() -> None:
    """Queries from an earlier analyzer are not this one's evidence."""
    audit = AuditLog(path=None)
    ctx = _ctx(audit)
    _log_query(audit, "earlier-analyzer-query")
    with ctx.scope() as scoped:
        _log_query(audit, "my-own-query")
        keys = scoped.evidence().query_keys
    assert "my-own-query" in keys
    assert "earlier-analyzer-query" not in keys, (
        "evidence reached backwards past its own analyzer; this is the defect"
    )


def test_two_scopes_do_not_borrow_each_other_evidence() -> None:
    """The shared-Context case, which is how `investigate` runs."""
    audit = AuditLog(path=None)
    ctx = _ctx(audit)
    with ctx.scope() as first:
        _log_query(audit, "query-a")
        a = first.evidence().query_keys
    with ctx.scope() as second:
        _log_query(audit, "query-b")
        b = second.evidence().query_keys
    assert a == ("query-a",)
    assert b == ("query-b",)


def test_the_scope_is_restored_afterwards() -> None:
    """Nesting must not strand the window open at an inner position."""
    audit = AuditLog(path=None)
    ctx = _ctx(audit)
    _log_query(audit, "before")
    with ctx.scope():
        pass
    # Outside any scope the window is the whole session, which is what an
    # attestation of a *run* wants.
    assert "before" in ctx.evidence().query_keys


def test_both_dispatch_sites_scope() -> None:
    """A new analyzer must not have to remember to do this itself."""
    from pathlib import Path

    for name in ("analyze.py", "investigate.py"):
        source = Path("src/chainscope/cli/commands") / name
        assert "ctx.scope()" in source.read_text(), f"{name} runs an analyzer unscoped"
