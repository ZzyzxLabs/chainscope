"""``chainscope report`` --- the case, written down, in one file.

A report is where an investigation either survives or does not, and the shape of
one that survives is not a list of findings. It is: here is what was concluded,
here is who concluded it, here is what they were looking at when they did, and
here is what they still do not know.

So this assembles four things that already existed separately and had never been
put in one place:

- the **narrative** from `case.db` --- observations, decisions, corrections, in
  the order somebody worked in, with the author of each;
- the **claims** from the store, resolved, with **who** asserted each and a
  marker where two of comparable strength disagree;
- the **boundary** --- the frontier, the addresses seen and never followed,
  which is the difference between "nothing further was found" and "nobody
  looked";
- the **provenance** --- the attestation, if one exists, and whether re-hashing
  the cache right now still matches it.

**Open questions go first, before the conclusions.** A report ordered the usual
way reads as finished no matter how much of it is unresolved, and the person
receiving it has no way to weigh that. Putting what is unknown at the top is the
one formatting decision here that changes what a reader believes.

**No graph.** `chainscope graph` already writes a self-contained interactive
file; embedding a copy would mean choosing a seed and a depth on the reader's
behalf, and an interactive canvas prints as a blank rectangle. Pass `--attach`
and the graph is listed as a companion artefact with its hash, which is what
ties it to this report.

**No PDF library.** The HTML carries a print stylesheet, so a browser's
print-to-PDF produces the artefact people actually need to attach to an e-mail.
A dependency that renders PDF server-side would be a large amount of surface
area to own for a job the browser already does correctly.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...case.correspondence import Ledger, Request
from ...case.log import CaseLog, Note, NoteKind
from ...core.attribution import Attribution, ResolvedEntity, merge
from ...render.base import Renderer

__all__ = ["add_parser", "run"]


def add_parser(sub: Any, name: str) -> None:
    p = sub.add_parser(name, help="assemble the case narrative, claims, and provenance")
    p.add_argument("--title", default="", help="case name. Defaults to the directory")
    p.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    p.add_argument("--case", type=Path, default=Path(".chainscope/case.db"))
    p.add_argument(
        "--attestation",
        type=Path,
        default=Path(".chainscope/attestation.json"),
        help="written by `chainscope attest`. Absent is reported, not fatal",
    )
    p.add_argument(
        "--attach",
        type=Path,
        action="append",
        default=[],
        help="a companion file --- a graph, an export --- listed with its hash. Repeatable",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("report.html"),
        help="`.md` writes markdown, anything else writes HTML with a print stylesheet",
    )


def run(args: argparse.Namespace, render: Renderer) -> int:
    case = _gather(args)
    if not (case["notes"] or case["entities"] or case["outstanding"] or case["answered"]):
        print(
            f"nothing to report: no notes or correspondence in {args.case} and no "
            f'attributions in {args.store}.\n  chainscope note observation "..."'
            f"          # start the narrative\n  chainscope tag <address> ..."
            f"                # record what something is\n  chainscope request "
            f'send "..." -k freeze    # start the clock on an exchange',
            file=sys.stderr,
        )
        return 2

    text = _markdown(case) if args.out.suffix.lower() == ".md" else _html(case)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")

    print(f"wrote {args.out}")
    print(
        f"  {len(case['notes'])} note(s) and {len(case['entities'])} address(es) "
        f"with a claim, from {len(case['contributors'])} contributor(s)"
    )
    if case["open"]:
        print(f"  {len(case['open'])} open question(s) --- reported first, before the findings")
    if case["outstanding"]:
        overdue = len(case["overdue"])
        tail = f", {overdue} past a deadline" if overdue else ""
        print(f"  {len(case['outstanding'])} request(s) still outstanding{tail}")
    if case["disputed"]:
        print(f"  {len(case['disputed'])} address(es) where sources of equal strength disagree")
    if case["unnamed"]:
        # Worth a line: it is the difference between a report a colleague can
        # act on and one where every claim is from "somebody".
        print(
            f"  {case['unnamed']} claim(s) with no analyst recorded --- imports and "
            f"heuristics, or tags made before this was tracked"
        )
    if args.out.suffix.lower() != ".md":
        print("\nopen it and print to PDF; the stylesheet is written for that")
    return 0


# ------------------------------------------------------------------ gathering


def _gather(args: argparse.Namespace) -> dict[str, Any]:
    log = CaseLog(args.case)
    try:
        notes = log.notes()
        open_questions = log.open_questions()
        analysts = log.analysts()
        replaced = log.superseded()
    finally:
        log.close()

    now = datetime.now(timezone.utc)
    ledger = Ledger(args.case)
    try:
        outstanding = ledger.requests(open_only=True)
        answered = [r for r in ledger.requests() if not r.is_open]
    finally:
        ledger.close()

    entities: list[ResolvedEntity] = []
    frontier = 0
    stats: Any = None
    unnamed = 0
    if args.store.exists():
        from ...store.sqlite import SqliteStore

        store = SqliteStore(args.store)
        try:
            stats = store.stats()
            rows = store._conn.execute("SELECT DISTINCT address FROM attributions").fetchall()
            by_address: dict[str, list[Attribution]] = {}
            for row in rows:
                claims = store.attributions(row["address"])
                if claims:
                    by_address[row["address"]] = claims
                    unnamed += sum(1 for c in claims if not c.analyst)
            for _, claims in sorted(by_address.items()):
                resolved = merge(claims)
                if resolved:
                    entities.append(resolved)
            frontier = sum(len(store.frontier(chain)) for chain in _chains(stats))
        finally:
            store.close()

    return {
        "contributors": _contributors(analysts, entities),
        "outstanding": sorted(outstanding, key=lambda r: -r.age_days(now)),
        "answered": answered,
        "overdue": [r for r in outstanding if r.overdue_at(now)],
        "now": now,
        "title": args.title or Path.cwd().name,
        "generated": now,
        "notes": notes,
        "open": open_questions,
        "replaced": replaced,
        "analysts": analysts,
        "entities": entities,
        "disputed": [e for e in entities if e.disputed],
        "unnamed": unnamed,
        "frontier": frontier,
        "stats": stats,
        "attestation": _attestation(args.attestation),
        "attachments": [_attached(p) for p in args.attach],
        "store": args.store,
        "case": args.case,
    }


def _contributors(
    analysts: list[tuple[str, str, int]], entities: list[ResolvedEntity]
) -> list[dict[str, Any]]:
    """Everyone who put something into this case, notes or claims.

    Counted from both, because they are different kinds of contribution and
    somebody can make either without the other. A list built from notes alone
    silently omits the colleague who labelled thirty addresses and wrote no
    narrative --- which in a shared case is the person most likely to be asked
    about a disagreement.
    """
    people: dict[str, dict[str, Any]] = {}
    for name, origin, count in analysts:
        people[name] = {"name": name, "origin": origin, "notes": count, "claims": 0}
    for entity in entities:
        for claim in entity.all_claims:
            if not claim.analyst:
                continue
            person = people.setdefault(
                claim.analyst,
                # No identity source: it is not recorded on a claim, only on a
                # note. Reported as unknown rather than assumed to be chosen.
                {"name": claim.analyst, "origin": "", "notes": 0, "claims": 0},
            )
            person["claims"] += 1
    return sorted(people.values(), key=lambda p: (-(p["notes"] + p["claims"]), p["name"]))


def _chains(stats: Any) -> list[Any]:
    from ...core.chainid import ChainId

    out = []
    for raw in getattr(stats, "chains", []) or []:
        try:
            out.append(ChainId.parse(raw))
        except Exception:
            # A chain string the parser does not recognise is worth skipping
            # rather than failing a report over; the count it feeds is a
            # coverage figure, not a claim.
            continue
    return out


def _attestation(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"unreadable": str(path)}
    return {
        "path": str(path),
        "attested": data.get("attested", ""),
        "responses": len(data.get("responses", {})),
        "queries": data.get("queries_recorded", 0),
        "missing": len(data.get("queries_without_a_cached_response", [])),
        "holes": data.get("unreadable_audit_lines", 0),
    }


def _attached(path: Path) -> dict[str, Any]:
    from .attest import digest_for

    if not path.exists():
        # Named anyway. A report listing an artefact that is not there says
        # something true; silently dropping it says nothing.
        return {"name": str(path), "sha256": "", "bytes": 0, "missing": True}
    return {
        "name": str(path),
        "sha256": digest_for(path),
        "bytes": path.stat().st_size,
        "missing": False,
    }


def _by_day(notes: list[Note]) -> list[tuple[str, list[Note]]]:
    groups: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        groups[note.at.strftime("%Y-%m-%d")].append(note)
    return sorted(groups.items())


_KIND_ORDER = {
    NoteKind.OBSERVATION: "observed",
    NoteKind.DECISION: "decided",
    NoteKind.QUESTION: "asked",
    NoteKind.CORRECTION: "corrected",
}


# ------------------------------------------------------------------ markdown


def _markdown(case: dict[str, Any]) -> str:
    out: list[str] = [f"# {case['title']}", ""]
    out.append(f"*Generated {case['generated'].strftime('%Y-%m-%d %H:%M')} UTC.*")
    out.append("")

    if case["open"] or case["outstanding"]:
        out += ["## Not yet known", "", _unknown_preamble(), ""]
        for note in case["open"]:
            out.append(
                f"- **[{note.id}]** {note.body} — *{note.analyst}, "
                f"{note.at.strftime('%Y-%m-%d')}*"
            )
        if case["outstanding"]:
            out += ["", "**Waiting on somebody else**", ""]
            for req in case["outstanding"]:
                out.append(f"- {_request_line(req, case['now'])}")
        out.append("")

    out += ["## Who worked on this", ""]
    if case["contributors"]:
        for person in case["contributors"]:
            out.append(f"- {person['name']} — {_contribution(person)}")
    else:
        out.append("*Nobody is recorded. Nothing here can say who concluded what.*")
    out.append("")

    out += ["## Narrative", ""]
    if case["notes"]:
        for day, notes in _by_day(case["notes"]):
            out += [f"### {day}", ""]
            for note in notes:
                struck = " *(superseded)*" if note.id in case["replaced"] else ""
                where = f" · `{note.subject}`" if note.subject else ""
                out.append(
                    f"**[{note.id}] {_KIND_ORDER[note.kind]}** — {note.analyst}{where}{struck}"
                )
                out += ["", f"> {note.body}", ""]
                if note.supersedes:
                    out += [f"*Replaces note {note.supersedes}.*", ""]
    else:
        out += ["*No notes. The reasoning behind this case is not recorded.*", ""]

    out += ["## Addresses", ""]
    if case["entities"]:
        out.append("| Address | Label | Category | Confidence | Source | Analyst |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for entity in case["entities"]:
            claim = entity.primary
            flag = " ⚠" if entity.disputed else ""
            out.append(
                f"| `{entity.address}` | {claim.label}{flag} | {claim.category.value} "
                f"| {claim.confidence.name.lower()} | {claim.source} "
                f"| {claim.analyst or '—'} |"
            )
        out.append("")
        if case["disputed"]:
            out += ["**Where sources disagree**", ""]
            for entity in case["disputed"]:
                out.append(f"- `{entity.address}`")
                for claim in entity.all_claims:
                    out.append(
                        f"  - {claim.label} ({claim.category.value}, "
                        f"{claim.confidence.name.lower()}) — {claim.source}"
                        f"{f', {claim.analyst}' if claim.analyst else ''}"
                    )
            out += ["", _dispute_preamble(), ""]
    else:
        out += ["*No attributions recorded.*", ""]

    if case["outstanding"] or case["answered"]:
        out += ["## Correspondence", "", _correspondence_preamble(), ""]
        out.append("| # | Sent to | Asked for | Age | Status |")
        out.append("| --- | --- | --- | --- | --- |")
        for req in case["outstanding"] + case["answered"]:
            status = req.status.value if req.status else "no reply"
            late = " ⚠" if req.overdue_at(case["now"]) else ""
            out.append(
                f"| {req.id} | {req.counterparty} | {req.kind.value} "
                f"| {req.age_days(case['now'])}d | {status}{late} |"
            )
        out.append("")

    out += ["## What this covers, and what it does not", "", _coverage(case), ""]
    out += ["## Provenance", "", _provenance(case), ""]

    if case["attachments"]:
        out += ["### Companion files", ""]
        for item in case["attachments"]:
            if item["missing"]:
                out.append(f"- `{item['name']}` — **not found when this was written**")
            else:
                out.append(
                    f"- `{item['name']}` — {item['bytes']:,} bytes, "
                    f"sha256 `{item['sha256'][:16]}…`"
                )
        out.append("")

    return "\n".join(out) + "\n"


def _contribution(person: dict[str, Any]) -> str:
    bits = []
    if person["notes"]:
        bits.append(f"{person['notes']} note(s)")
    if person["claims"]:
        bits.append(f"{person['claims']} claim(s)")
    text = ", ".join(bits)
    if person["origin"] == "os":
        # The one case worth flagging in a report somebody else will read: a
        # local machine account is a name nobody chose and may belong to nobody
        # in particular on the machine it is read on.
        text += "  **(OS account, unverified)**"
    return text


def _request_line(request: Request, now: datetime) -> str:
    status = request.status.value if request.status else "no reply yet"
    line = (
        f"**{request.kind.value}** to {request.counterparty} — sent "
        f"{request.sent_at:%Y-%m-%d}, {request.age_days(now)}d ago, {status}"
    )
    if request.subject:
        line += f" (`{request.subject}`)"
    if request.due_at and request.overdue_at(now):
        line += f" — **{(now - request.due_at).days}d past its deadline**"
    return line


def _correspondence_preamble() -> str:
    return (
        "What was asked of whom, and whether it came back. A request nobody has "
        "answered is not a request that was refused: only the second is a "
        "decision somebody made, and only the second can be escalated against. "
        "Age stops at the reply, so an open request is always the older one."
    )


def _unknown_preamble() -> str:
    return (
        "These are open. A report ordered findings-first reads as finished no "
        "matter how much of it is not, and a reader has no way to weigh that."
    )


def _dispute_preamble() -> str:
    return (
        "Both claims are kept. Nothing here picks a winner between two sources "
        "of comparable strength — that is a judgement for a person, and it "
        "belongs in the narrative as a decision, where it carries a name."
    )


def _coverage(case: dict[str, Any]) -> str:
    stats = case["stats"]
    if stats is None:
        return f"No store at `{case['store']}`. Nothing was traced through this tool."
    lines = [
        f"- {stats.transfers:,} transfers over {stats.addresses:,} addresses "
        f"on {', '.join(stats.chains) or 'no chain'}",
        f"- {stats.attributions:,} attribution claim(s)",
    ]
    if case["frontier"]:
        lines.append(
            f"- **{case['frontier']:,} addresses were seen and never followed.** "
            f"That is the boundary of this case, not the end of the money. An "
            f"address on the frontier had nothing checked past it; it is not an "
            f"address where nothing was found."
        )
    else:
        lines.append(
            "- No frontier: every address seen was expanded, or the traversal never ran."
        )
    return "\n".join(lines)


def _provenance(case: dict[str, Any]) -> str:
    att = case["attestation"]
    if att is None:
        return (
            "**No attestation.** The figures above are not bound to the responses "
            "that produced them, so a reader cannot check that the underlying data "
            "has not changed since. Run `chainscope attest` and regenerate this."
        )
    if "unreadable" in att:
        return f"An attestation exists at `{att['unreadable']}` but is not readable JSON."
    lines = [
        f"- {att['responses']} cached response(s) hashed, {att['queries']} quer(ies) recorded",
        f"- attested {att['attested']}",
    ]
    if att["missing"]:
        lines.append(
            f"- {att['missing']} quer(ies) have no cached response — uncacheable, "
            f"or pruned since"
        )
    if att["holes"]:
        lines.append(f"- **{att['holes']} unreadable audit line(s): the record has holes**")
    lines.append(
        "\nVerify with `chainscope attest --verify`. That is a manifest, not a "
        "signature: it catches drift and accident, not somebody with write access "
        "to the case directory."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------- html

_CSS = """
:root { --ink:#1a1a1a; --dim:#666; --line:#ddd; --warn:#b23; --paper:#fff; }
@media (prefers-color-scheme: dark) {
  :root { --ink:#e8e8e8; --dim:#999; --line:#333; --warn:#f77; --paper:#141414; }
}
* { box-sizing: border-box; }
body { font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       color: var(--ink); background: var(--paper); margin: 0; }
main { max-width: 46rem; margin: 0 auto; padding: 3rem 1.5rem 6rem; }
h1 { font-size: 1.8rem; margin: 0 0 .3rem; }
h2 { font-size: 1.15rem; margin: 3rem 0 .8rem; padding-bottom: .3rem;
     border-bottom: 1px solid var(--line); }
h3 { font-size: .82rem; text-transform: uppercase; letter-spacing: .08em;
     color: var(--dim); margin: 2rem 0 .6rem; font-weight: 600; }
.meta { color: var(--dim); font-size: .87rem; margin: 0 0 1rem; }
.lede { color: var(--dim); font-size: .92rem; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; }
.note { margin: 0 0 1.4rem; padding-left: .9rem; border-left: 2px solid var(--line); }
.note.superseded { opacity: .55; }
.note.superseded .body { text-decoration: line-through; text-decoration-color: var(--dim); }
.note .who { font-size: .78rem; color: var(--dim); letter-spacing: .02em; }
.note .body { margin: .25rem 0 0; white-space: pre-wrap; }
.kind { display: inline-block; font-size: .7rem; text-transform: uppercase;
        letter-spacing: .07em; padding: .05rem .4rem; border: 1px solid var(--line);
        border-radius: 3px; margin-right: .4rem; }
.open li { margin-bottom: .5rem; }
table { border-collapse: collapse; width: 100%; font-size: .84rem; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--dim); }
/* A 42-character address in a monospace column is wide enough to push the
   analyst off the right edge, which is the one column somebody is reading the
   table for. Wrapping the address costs two lines; losing the name costs the
   point of the table. */
td.mono { word-break: break-all; max-width: 15rem; }
.scroll { overflow-x: auto; }
.warn { color: var(--warn); font-weight: 600; }
.none { color: var(--dim); font-style: italic; }
ul { padding-left: 1.1rem; }
@media print {
  :root { --ink:#000; --dim:#555; --line:#bbb; --paper:#fff; }
  body { font-size: 11pt; }
  main { max-width: none; padding: 0; }
  h2 { break-after: avoid; }
  .note, tr { break-inside: avoid; }
}
"""


def _e(text: object) -> str:
    return html.escape(str(text))


def _html(case: dict[str, Any]) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_e(case['title'])}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>{_e(case['title'])}</h1>",
        f"<p class=meta>Generated {case['generated'].strftime('%Y-%m-%d %H:%M')} UTC</p>",
    ]

    if case["open"] or case["outstanding"]:
        parts.append("<h2>Not yet known</h2>")
        parts.append(f"<p class=lede>{_e(_unknown_preamble())}</p>")
        if case["open"]:
            parts.append("<ul class=open>")
            for note in case["open"]:
                parts.append(
                    f"<li>{_e(note.body)} <span class=who>— {_e(note.analyst)}, "
                    f"{note.at.strftime('%Y-%m-%d')} (note {note.id})</span></li>"
                )
            parts.append("</ul>")
        if case["outstanding"]:
            parts.append("<h3>Waiting on somebody else</h3><ul class=open>")
            for req in case["outstanding"]:
                parts.append(f"<li>{_inline(_request_line(req, case['now']))}</li>")
            parts.append("</ul>")

    parts.append("<h2>Who worked on this</h2>")
    if case["contributors"]:
        parts.append("<ul>")
        for person in case["contributors"]:
            parts.append(f"<li>{_e(person['name'])} — {_inline(_contribution(person))}</li>")
        parts.append("</ul>")
    else:
        parts.append(
            "<p class=none>Nobody is recorded. Nothing here can say who concluded what.</p>"
        )

    parts.append("<h2>Narrative</h2>")
    if case["notes"]:
        for day, notes in _by_day(case["notes"]):
            parts.append(f"<h3>{_e(day)}</h3>")
            for note in notes:
                gone = " superseded" if note.id in case["replaced"] else ""
                where = f" · <code>{_e(note.subject)}</code>" if note.subject else ""
                extra = f" · replaces note {note.supersedes}" if note.supersedes else ""
                parts.append(
                    f'<div class="note{gone}">'
                    f"<div class=who><span class=kind>{_e(note.kind.value)}</span>"
                    f"{_e(note.analyst)} · {note.at.strftime('%H:%M')}"
                    f"{where}{extra} · note {note.id}</div>"
                    f"<p class=body>{_e(note.body)}</p></div>"
                )
    else:
        parts.append(
            "<p class=none>No notes. The reasoning behind this case is not recorded.</p>"
        )

    parts.append("<h2>Addresses</h2>")
    if case["entities"]:
        parts.append(
            "<div class=scroll><table><thead><tr><th>Address<th>Label<th>Category"
            "<th>Confidence<th>Source<th>Analyst</tr></thead><tbody>"
        )
        for entity in case["entities"]:
            claim = entity.primary
            flag = " <span class=warn>⚠</span>" if entity.disputed else ""
            parts.append(
                f"<tr><td class=mono>{_e(entity.address)}<td>{_e(claim.label)}{flag}"
                f"<td>{_e(claim.category.value)}<td>{_e(claim.confidence.name.lower())}"
                f"<td>{_e(claim.source)}"
                f"<td>{_e(claim.analyst) if claim.analyst else '<span class=none>—</span>'}"
                "</tr>"
            )
        parts.append("</tbody></table></div>")
        if case["disputed"]:
            parts.append("<h3>Where sources disagree</h3><ul>")
            for entity in case["disputed"]:
                parts.append(f"<li><code>{_e(entity.address)}</code><ul>")
                for claim in entity.all_claims:
                    who = f", {_e(claim.analyst)}" if claim.analyst else ""
                    parts.append(
                        f"<li>{_e(claim.label)} ({_e(claim.category.value)}, "
                        f"{_e(claim.confidence.name.lower())}) — {_e(claim.source)}{who}</li>"
                    )
                parts.append("</ul></li>")
            parts.append(f"</ul><p class=lede>{_e(_dispute_preamble())}</p>")
    else:
        parts.append("<p class=none>No attributions recorded.</p>")

    if case["outstanding"] or case["answered"]:
        parts.append("<h2>Correspondence</h2>")
        parts.append(f"<p class=lede>{_e(_correspondence_preamble())}</p>")
        parts.append(
            "<div class=scroll><table><thead><tr><th>#<th>Sent to<th>Asked for"
            "<th>Age<th>Status</tr></thead><tbody>"
        )
        for req in case["outstanding"] + case["answered"]:
            status = req.status.value if req.status else "no reply"
            late = " <span class=warn>⚠</span>" if req.overdue_at(case["now"]) else ""
            parts.append(
                f"<tr><td>{req.id}<td>{_e(req.counterparty)}<td>{_e(req.kind.value)}"
                f"<td>{req.age_days(case['now'])}d<td>{_e(status)}{late}</tr>"
            )
        parts.append("</tbody></table></div>")

    parts.append("<h2>What this covers, and what it does not</h2>")
    parts.append(_bullets(_coverage(case)))
    parts.append("<h2>Provenance</h2>")
    parts.append(_bullets(_provenance(case)))

    if case["attachments"]:
        parts.append("<h3>Companion files</h3><ul>")
        for item in case["attachments"]:
            if item["missing"]:
                parts.append(
                    f"<li><code>{_e(item['name'])}</code> — "
                    f"<span class=warn>not found when this was written</span></li>"
                )
            else:
                parts.append(
                    f"<li><code>{_e(item['name'])}</code> — {item['bytes']:,} bytes, "
                    f"sha256 <span class=mono>{_e(item['sha256'][:16])}…</span></li>"
                )
        parts.append("</ul>")

    parts.append("</main></body></html>")
    return "\n".join(parts)


def _bullets(markdown: str) -> str:
    """Render the shared markdown prose blocks as HTML.

    The two blocks are authored once and used by both formats, so a sentence
    cannot be corrected in one output and left wrong in the other --- and these
    are exactly the sentences where that would matter.
    """
    out, items = [], []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(_inline(stripped[2:]))
            continue
        if items:
            out.append("<ul><li>" + "</li><li>".join(items) + "</li></ul>")
            items = []
        if stripped:
            out.append(f"<p>{_inline(stripped)}</p>")
    if items:
        out.append("<ul><li>" + "</li><li>".join(items) + "</li></ul>")
    return "".join(out)


def _inline(text: str) -> str:
    """`code` and **bold**, escaped first so the markup cannot come from data."""
    escaped = _e(text)
    for marker, tag in (("**", "strong"), ("`", "code")):
        chunks = escaped.split(marker)
        rebuilt = [chunks[0]]
        for i, chunk in enumerate(chunks[1:], 1):
            rebuilt.append(f"<{tag}>{chunk}</{tag}>" if i % 2 else chunk)
        escaped = "".join(rebuilt)
    return escaped
