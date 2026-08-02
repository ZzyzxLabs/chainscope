"""``chainscope attest`` --- bind a figure to the queries that produced it.

The pieces were all here and nothing joined them. `AuditLog` records every
query with its cache key. `Result.evidence` carries the keys a conclusion drew
on. The cache holds the responses. What was missing is the sentence that makes
a report defensible: *this number rests on these fourteen responses, and here
is a hash of each so you can check they have not moved.*

Without it "the tool said so" is the whole provenance, and that is not
provenance --- it is a claim about a claim.

**What an attestation is and is not.** It is a manifest: the queries, when they
ran, which provider answered, and a hash of each response as it sits in the
cache now. Re-running `attest --verify` re-hashes and reports any that differ.

It is **not** a signature and does not pretend to be. Anyone who can edit the
cache can edit the manifest beside it. What it defends against is drift and
accident --- a re-fetch that quietly returned something else, a cache rebuilt
between the analysis and the report --- not an adversary with write access to
the case directory. Saying which of the two is the whole value; a file called
"attestation" that implied the second would be worse than none.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...render.base import Renderer

__all__ = ["add_parser", "compare", "digest_for", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(
        name,
        help="record which cached responses a case rests on, or check they have not moved",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=Path(".chainscope/cache.db"),
        help="the response cache to hash. A SQLite file --- the same one "
        "CHAINSCOPE_CACHE_DIR names, despite what it is called",
    )
    p.add_argument(
        "--audit",
        type=Path,
        default=Path(".chainscope/audit.jsonl"),
        help="the audit log naming the queries",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(".chainscope/attestation.json"),
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="re-hash and report drift instead of writing. Exits 1 if anything moved",
    )


def digest_for(path: Path) -> str:
    """SHA-256 of a cached response, as it is on disk.

    Of the bytes, not of a parsed form. A parser that changes how it renders a
    field would otherwise show up as evidence tampering, and a real change that
    the parser normalises away would not show up at all.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _queries(audit: Path) -> list[dict[str, Any]]:
    """Every query the audit log records, newest last."""
    if not audit.exists():
        return []
    rows = []
    for line in audit.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            # A malformed line is a hole in the record and is reported as one.
            # Skipping silently would let a truncated log look complete.
            rows.append({"_unreadable": line[:120]})
    return rows


class NotACache(RuntimeError):
    """The path given is not a response cache this can read."""


def _responses(cache: Path) -> dict[str, dict[str, Any]]:
    """Every cached response, keyed by cache key, hashed as stored.

    The cache is a **SQLite file** with an `entries` table --- not a directory
    of response files. This function scanned a directory, which meant it found
    nothing against a real cache: zero responses hashed, every audited query
    reported as having no cached response, and `--verify` structurally unable
    to detect drift. The command was a no-op in its documented workflow, and it
    passed its own tests because the fixture was built to match the assumption
    rather than the interface.

    Hence :class:`NotACache`: a path that is not a readable cache raises rather
    than returning an empty dict. An attestation over nothing is worse than a
    refusal, because it produces a file that looks like provenance.
    """
    if not cache.exists():
        raise NotACache(f"no cache at {cache}")
    if cache.is_dir():
        raise NotACache(
            f"{cache} is a directory. The response cache is a single SQLite "
            f"file --- the setting is spelled CHAINSCOPE_CACHE_DIR but names a "
            f"file, which is confusing and is not something this command can "
            f"paper over."
        )
    try:
        conn = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
        rows = conn.execute("SELECT key, value FROM entries").fetchall()
    except sqlite3.Error as exc:
        raise NotACache(f"{cache} is not a readable response cache: {exc}") from None
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out: dict[str, dict[str, Any]] = {}
    for key, value in rows:
        # The stored bytes, not a parsed form --- the same reasoning as
        # `digest_for`: a parser that changed how it renders a field would
        # otherwise look like tampering.
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        out[str(key)] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return out


def run(args: argparse.Namespace, render: Renderer) -> int:
    queries = _queries(args.audit)
    unreadable = sum(1 for q in queries if "_unreadable" in q)

    try:
        entries = _responses(args.cache)
    except NotACache as exc:
        if not args.cache.exists() and not queries:
            # Cold start: neither exists, and the usual cause is that neither
            # environment variable was set. Naming them is more use than
            # naming the missing file.
            print(
                f"nothing to attest: no audit log at {args.audit} and no cache "
                f"at {args.cache}.\nRun an analysis with CHAINSCOPE_AUDIT_LOG "
                f"and CHAINSCOPE_CACHE_DIR set first.",
                file=sys.stderr,
            )
        else:
            print(f"cannot attest: {exc}", file=sys.stderr)
        return 2

    if not queries and not entries:
        print(
            f"nothing to attest: no audit log at {args.audit} and no responses "
            f"in {args.cache}.\nRun an analysis with CHAINSCOPE_AUDIT_LOG and "
            f"CHAINSCOPE_CACHE_DIR set first.",
            file=sys.stderr,
        )
        return 2

    if queries and not entries:
        # The shape the directory-scanning bug produced. An attestation over
        # zero responses is a file that looks like provenance and is not, so it
        # is refused rather than written.
        print(
            f"{len(queries)} quer(ies) are recorded in {args.audit} and "
            f"{args.cache} holds no responses.\nRefusing to write an "
            f"attestation that binds nothing --- check the cache path.",
            file=sys.stderr,
        )
        return 2

    # A query whose response is not in the cache is named. It is the ordinary
    # case for anything uncacheable, and it is also what a pruned cache looks
    # like --- and the difference matters when somebody asks what a figure
    # rests on.
    missing = sorted(
        {
            str(q.get("cache_key"))
            for q in queries
            if q.get("cache_key") and str(q.get("cache_key")) not in entries
        }
    )

    if args.verify:
        return _verify(args.out, entries, missing, unreadable)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "attested": datetime.now(timezone.utc).isoformat(),
                "cache": str(args.cache),
                "audit": str(args.audit),
                "queries_recorded": len(queries),
                "responses_hashed": len(entries),
                "queries_without_a_cached_response": missing,
                "unreadable_audit_lines": unreadable,
                "responses": entries,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    print(f"{len(entries)} cached response(s) hashed from {args.cache}")
    print(f"{len(queries)} quer(ies) recorded in {args.audit}")
    if missing:
        print(f"  {len(missing)} quer(ies) have no cached response")
    if unreadable:
        print(f"  {unreadable} unreadable audit line(s) --- the record has holes")
    print(f"\nwrote {args.out}")
    print("  chainscope attest --verify   # check nothing has moved since")
    print(
        "\nThis is a manifest, not a signature. It catches drift --- a re-fetch "
        "that\nreturned something else, a cache rebuilt between the analysis and "
        "the report.\nIt does not defend against anyone who can write to this "
        "directory."
    )
    return 0


def compare(
    recorded: dict[str, dict[str, Any]],
    now: dict[str, dict[str, Any]],
    *,
    uncached: Sequence[str] = (),
    unreadable: int = 0,
) -> dict[str, Any]:
    """What moved between an attestation and the cache as it is now.

    A value rather than printed text, so the decision --- what counts as drift
    --- can be asserted on directly. Reading it back out of stdout tests the
    wording rather than the judgement, and the wording is the part that is
    allowed to change.
    """
    return {
        # The serious one: the same query, a different answer, and no record of
        # anybody deciding that.
        "changed": sorted(
            k for k in recorded if k in now and now[k]["sha256"] != recorded[k]["sha256"]
        ),
        "gone": sorted(set(recorded) - set(now)),
        # Not drift. Work continued; reported so the count reconciles.
        "new": sorted(set(now) - set(recorded)),
        "unchanged": sorted(
            k for k in recorded if k in now and now[k]["sha256"] == recorded[k]["sha256"]
        ),
        "uncached": list(uncached),
        "unreadable_audit_lines": unreadable,
    }


def _verify(
    out: Path, now: dict[str, dict[str, Any]], missing: list[str], unreadable: int
) -> int:
    if not out.exists():
        print(f"no attestation at {out} to verify against", file=sys.stderr)
        return 2
    try:
        recorded = json.loads(out.read_text(encoding="utf-8")).get("responses", {})
    except ValueError:
        print(f"{out} is not readable JSON", file=sys.stderr)
        return 2

    result = compare(recorded, now, uncached=missing, unreadable=unreadable)

    for key in result["changed"]:
        print(f"CHANGED  {key}")
    for key in result["gone"]:
        print(f"MISSING  {key}  (was attested, is not in the cache now)")
    for key in result["new"]:
        print(f"new      {key}")

    # Reported on every run, not only a clean one. These are holes in the
    # record, and a verify that printed "unchanged" while staying quiet about
    # them would answer a narrower question than the one being asked.
    if result["uncached"]:
        print(
            f"{len(result['uncached'])} quer(ies) have no cached response --- "
            f"uncacheable, or pruned since"
        )
    if result["unreadable_audit_lines"]:
        print(
            f"{result['unreadable_audit_lines']} unreadable audit line(s) --- "
            f"the record has holes"
        )

    if not result["changed"] and not result["gone"]:
        print(f"{len(recorded)} attested response(s) unchanged.")
        if result["new"]:
            print(
                f"{len(result['new'])} added since --- re-run without --verify to attest them."
            )
        # Zero even with holes above. A missing or unreadable line is a gap in
        # what was recorded, not evidence that a recorded response moved, and
        # failing on it would make the exit code answer two questions at once.
        return 0

    print(
        f"\n{len(result['changed'])} changed and {len(result['gone'])} missing. Any "
        f"figure resting on those\nwas computed from something other than what is "
        f"here now.",
        file=sys.stderr,
    )
    return 1
