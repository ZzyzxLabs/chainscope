"""The pages around the tool: what this is, what each view shows, how to ask.

Every visualisation this package produces was reachable only if you already
knew it existed. `graph -f flow` writes one file, `dashboard` writes another,
`report` a third, and the way to browse them was whatever directory listing the
shell gave you --- a page that names files and explains nothing. Somebody who
did not write this tool cannot tell a flow graph from a Cytoscape export from a
case dashboard by filename, and the whole point of the project is that another
person can pick it up.

So three audiences get a front door here, and they are genuinely different:

**A person** gets `/` (the tool) and `/docs` --- every view named, with what it
is *for* and what it cannot tell you. The second half matters more: a graph
that stopped at a node limit and a graph that reached the end of the money look
identical, and only the docs can say which questions each view answers honestly.

**An agent** gets `/llms.txt` and `/.well-known/agent.json` --- the llms.txt
convention and an A2A-style card, so a model can discover the endpoints without
a human pasting API docs into a prompt. These describe the *same* surface the
MCP server exposes, because two descriptions of one tool drift apart.

**A reader in a hurry** gets the plain fact that this is a local, offline,
loopback tool holding somebody's case, and that nothing here phones out.

Nothing on these pages fetches, from a chain or anywhere else. They are strings
in this module, served by the same loopback server, for the same reason the app
inlines its CSS: a forensics tool that loads a font from a CDN tells that CDN
which case is open.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["VIEWS", "agent_card", "docs_page", "landing_page", "llms_txt"]


#: Every view this package can produce, in the order somebody meets them.
#:
#: The ``limits`` field is not decoration. Each of these can produce a picture
#: that looks complete when it is not, and the specific way it fails differs
#: per view --- so it is recorded next to the view rather than in one general
#: disclaimer nobody reads twice.
VIEWS: tuple[dict[str, str], ...] = (
    {
        "name": "flow graph",
        "where": "the middle of this page, or `chainscope graph -f flow`",
        "what": (
            "Addresses as cards, money as arrows, laid out left to right by how "
            "many hops the funds travelled from the seed."
        ),
        "for": (
            "Reading the shape of a path: a split into many wallets, a "
            "collection back into one, a hop through a mixer."
        ),
        "limits": (
            "The columns are hop distance, not time \u2014 an address two columns "
            "right is not necessarily later. A node drawn with a dashed border "
            "is a frontier: its counterparties were never fetched, so the "
            "picture stops there because nobody looked, not because the money "
            "did."
        ),
    },
    {
        "name": "timeline scrubber",
        "where": "bottom left of the graph",
        "what": "Hides flows whose span had not yet reached the chosen instant.",
        "for": "Watching an incident unfold, and seeing what was already true before it.",
        "limits": (
            "An edge aggregates many transfers, so it appears once its span "
            "reaches the cursor. Undated edges are always shown: a missing "
            "timestamp means the provider gave none, which says nothing about when."
        ),
    },
    {
        "name": "asset filter",
        "where": "left panel",
        "what": (
            "Every asset in view, grouped by whether its symbol matches the "
            "canonical contract for that symbol on this chain."
        ),
        "for": (
            "Seeing at a glance that a transfer labelled USDC was not USDC. "
            "Forged tokens are how a graph is made to tell a false story."
        ),
        "limits": (
            "UNLISTED means there is no canonical entry to compare against \u2014 "
            "it is neither an accusation nor a clearance."
        ),
    },
    {
        "name": "follow the money from here",
        "where": "right panel, with an address selected",
        "what": (
            "Fetches one hop out from the selected address and merges it in, "
            "filtered by direction, asset, time window and value floor."
        ),
        "for": (
            "Building the picture by decision rather than by depth: every "
            "address on screen is there because you judged the one before it "
            "worth following."
        ),
        "limits": (
            "The only control here that spends a rate limit. A filter narrows "
            "what is fetched, so it narrows what you will ever see \u2014 the "
            "result states how many flows it excluded, because a filter that "
            "matched nothing and an address that never moved money produce the "
            "same small graph."
        ),
    },
    {
        "name": "case dashboard",
        "where": "`chainscope dashboard`",
        "what": "A case overview: what is labelled, what is open, what was asked of whom.",
        "for": "Picking up a case after a week away, or handing one to somebody else.",
        "limits": "Reads the store only. It cannot know about work done outside it.",
    },
    {
        "name": "report",
        "where": "`chainscope report`",
        "what": (
            "The case narrative, its claims, and the provenance of each \u2014 "
            "assembled from notes, attributions and analyzer results."
        ),
        "for": "Handing conclusions to somebody who will act on them.",
        "limits": (
            "Every claim carries its confidence and source. A report whose "
            "claims are all MEDIUM is a report of leads, not of evidence, and "
            "reads that way on purpose."
        ),
    },
    {
        "name": "case bundle",
        "where": "`chainscope bundle export`",
        "what": (
            "One directory or zip holding the case record, the query cache, the "
            "audit log, the analyzer results and the report, under one manifest."
        ),
        "for": "Sending a case to somebody who can then replay and check it.",
        "limits": (
            "A bundle without the query cache documents what was concluded but "
            "not from what, and says so rather than looking complete."
        ),
    },
    {
        "name": "graph exports",
        "where": "`chainscope graph -f {cytoscape,d3,dot,gexf}`",
        "what": "The same graph in formats other tools read.",
        "for": "Taking the case into Gephi, Cytoscape, or a d3 page of your own.",
        "limits": (
            "These carry the topology and the amounts, not the provenance. A "
            "claim rendered in another tool loses the confidence attached to it "
            "here, which is exactly the flattening this package exists to resist."
        ),
    },
)


#: What an agent can call. Mirrors the MCP tool surface deliberately: two
#: descriptions of one tool drift, and the drift always favours the one nobody
#: reads.
_SKILLS: tuple[dict[str, Any], ...] = (
    {
        "id": "resolve-address",
        "name": "Say what is known about an address",
        "description": (
            "Every attribution held for an address, each with its source, "
            "confidence and rationale. Reports which sources could not be "
            "consulted, so an empty answer is never mistaken for a clean one."
        ),
        "http": "GET /resolve?address=&chain=",
    },
    {
        "id": "flows",
        "name": "List the money in or out of an address",
        "description": (
            "Aggregated flows by counterparty and asset, largest first. "
            "Amounts are strings: JSON numbers are doubles and would round."
        ),
        "http": "GET /flows?address=&chain=&direction=out|in",
    },
    {
        "id": "expand",
        "name": "Follow the money one hop",
        "description": (
            "Fetch a single address's counterparties from a chain and merge "
            "them into the case, filtered by direction, asset, time and value. "
            "The one call here that spends a rate limit. Returns what it "
            "excluded alongside what it kept."
        ),
        "http": "GET /expand?address=&chain=&direction=out,in&asset=&since=&min_raw=",
    },
    {
        "id": "graph",
        "name": "Get the flow graph around an address",
        "description": (
            "Nodes, edges, hop depths and assets, built from the store. Marks "
            "frontier nodes, so a boundary is never read as an ending."
        ),
        "http": "GET /graph?address=&chain=&depth=&max_nodes=",
    },
    {
        "id": "analyze",
        "name": "Run an analyzer",
        "description": (
            "impersonation, poisoning, contributors or route. Returns findings "
            "(observations) and hypotheses (inferences) separately, with the "
            "factors behind each score exposed rather than a single number."
        ),
        "http": "GET /analyze?name=&address=&chain=",
    },
    {
        "id": "case-record",
        "name": "Read and add to the case record",
        "description": (
            "Notes and leads. Append-only, authored and timed: an agent's "
            "suggestion stays distinguishable from a person's judgement six "
            "months later, which cannot be recovered after the fact."
        ),
        "http": "GET /notes, GET /leads, POST /note, POST /tag",
    },
)


def landing_page(has_store: bool, store: str, transfers: int) -> str:
    """The front door: what this is, and the honest state of this store.

    Shown instead of an empty canvas when no address has been chosen. An empty
    graph and a graph of an address with no counterparties look the same, and a
    first-time reader has no way to tell which they are looking at --- so the
    empty state says which, in numbers.
    """
    state = (
        f"<b>{transfers:,}</b> transfer(s) in <code>{_esc(store)}</code>"
        if has_store and transfers
        else f"<b>nothing yet</b> in <code>{_esc(store)}</code> &mdash; "
        f"select an address and expand it, or run "
        f"<code>chainscope investigate &lt;address&gt;</code>"
    )
    cards = "".join(
        f"<article><h3>{_esc(v['name'])}</h3>"
        f'<p class="where"><code>{_esc(v["where"])}</code></p>'
        f"<p>{_esc(v['what'])}</p>"
        f'<p class="for"><b>Use it to</b> {_esc(v["for"])}</p>'
        f'<p class="lim"><b>It cannot tell you</b> {_esc(v["limits"])}</p></article>'
        for v in VIEWS
    )
    return f"""<section id="landing">
  <h2>chainscope</h2>
  <p class="lede">A case you can hand to somebody else. Every claim carries
  where it came from and how sure it is, and every absence says whether it is
  an absence of evidence or an absence of looking.</p>
  <p class="state">{state}</p>
  <p class="lede">Type an address above to open it. Nothing here reaches the
  network until you press <em>follow the money from here</em> on a selected
  address.</p>
  <div class="views">{cards}</div>
  <p class="foot"><a href="/docs">Full documentation</a> &middot;
  <a href="/llms.txt">llms.txt</a> &middot;
  <a href="/.well-known/agent.json">agent card</a></p>
</section>"""


def docs_page(origin: str) -> str:
    """Human documentation: every view, every endpoint, and what each cannot say."""
    views = "".join(
        f"<section><h3>{_esc(v['name'])}</h3>"
        f'<p class="where">{_esc(v["where"])}</p>'
        f"<p>{_esc(v['what'])}</p>"
        f"<p><b>Use it to</b> {_esc(v['for'])}</p>"
        f'<p class="lim"><b>It cannot tell you</b> {_esc(v["limits"])}</p></section>'
        for v in VIEWS
    )
    skills = "".join(
        f"<section><h3><code>{_esc(s['http'])}</code></h3>"
        f"<p><b>{_esc(s['name'])}</b> &mdash; {_esc(s['description'])}</p></section>"
        for s in _SKILLS
    )
    return f"""<!doctype html><meta charset="utf-8">
<title>chainscope — documentation</title>
<style>{_DOC_CSS}</style>
<main>
<h1>chainscope</h1>
<p class="lede">Open-source blockchain forensics that refuses to flatten
uncertainty. Heuristic output is a lead, not evidence, and the tool says which
at every point rather than leaving you to remember.</p>

<h2>The one rule everything here follows</h2>
<p>An absence must never be indistinguishable from a result. A source that
could not be read, a walk that hit a node limit, a filter that matched nothing,
and a window that missed the transfer all produce an empty answer &mdash; and
each is reported as itself, because only one of them means &ldquo;there is
nothing there&rdquo;.</p>

<h2>The views</h2>
{views}

<h2>The HTTP surface</h2>
<p>Loopback only, token-authenticated, same-origin. Every response that could
be partial says so in a field rather than by being shorter.</p>
{skills}

<h2>For agents</h2>
<p>An MCP server exposes this same surface as tools
(<code>chainscope-mcp</code>). Machine-readable discovery lives at
<a href="{_esc(origin)}/llms.txt">/llms.txt</a> and
<a href="{_esc(origin)}/.well-known/agent.json">/.well-known/agent.json</a>.</p>
<p>Three properties are built in rather than left to prompting: every claim
carries its provenance as fields on the same object; writing is opt-in and
records which agent wrote it; and amounts cross the boundary as strings,
because JSON numbers are doubles and 10 ETH already exceeds what one holds
exactly.</p>

<h2>Privacy</h2>
<p>No CDN, no fonts, no telemetry, no map tiles. A forensics tool that phones a
third party on load tells that third party which addresses are being
investigated. The server binds to <code>127.0.0.1</code> and the page inlines
everything it needs.</p>
</main>"""


def llms_txt(origin: str) -> str:
    """The llms.txt index: what this is and where an agent should look."""
    skills = "\n".join(
        f"- `{s['http']}` — **{s['name']}**: {s['description']}" for s in _SKILLS
    )
    views = "\n".join(f"- **{v['name']}** ({v['where']}): {v['what']}" for v in VIEWS)
    return f"""# chainscope

> Open-source blockchain forensics with provenance in the types. Every claim
> carries its source and confidence; every absence states whether it is an
> absence of evidence or an absence of looking. Runs locally against a case
> store; loopback only; never phones a third party.

This file is a concise index for AI agents. The same surface is available as
MCP tools via `chainscope-mcp`.

## The rule that governs every response

An absence must never be indistinguishable from a result. A source that could
not be read, a walk stopped by a node limit, a filter that matched nothing and
a time window that missed the transfer all yield an empty answer, and each is
reported as itself. When you summarise a result, carry that distinction --- a
tool whose output can be paraphrased into certainty is worse than no tool.

## Reading the output

- **Findings are observations. Hypotheses are inferences.** They are separate
  fields and must not be merged in a summary.
- **Confidence is a field, not a tone.** `MEDIUM` from a community list is not
  `CERTAIN` from a sanctions list, and a hypothesis never exceeds `MEDIUM`.
- **Amounts are strings.** They are wei-scale; parsing them as JSON numbers
  rounds them.
- **`frontier: true` means nobody looked**, not that the money stopped.
- **`truncated` / `complete: false` means there was more.** Do not report the
  rows you got as the whole answer.

## HTTP endpoints

{skills}

## Views

{views}

## Documentation

- [Documentation]({origin}/docs) — every view, what it is for, and what it
  cannot tell you.
- [Agent card]({origin}/.well-known/agent.json) — A2A-style capability
  description.
"""


def agent_card(origin: str, writable: bool) -> str:
    """An A2A-style card, so another agent can discover this one."""
    card = {
        "protocolVersion": "0.2.0",
        "name": "chainscope",
        "description": (
            "Blockchain forensics over a local case store. Answers about "
            "addresses, flows and attribution, with the provenance and "
            "confidence of every claim attached, and with partial or failed "
            "results reported as such rather than as smaller answers."
        ),
        "url": origin,
        "preferredTransport": "HTTP+JSON",
        "version": "0.1.0",
        "provider": {"organization": "chainscope", "url": origin},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "tags": ["forensics", "blockchain", "attribution"],
                "http": f"{origin} {s['http']}",
            }
            for s in _SKILLS
        ],
        # The parts a caller has to honour to use this honestly. Stated in the
        # card because an agent that discovers the endpoints without them will
        # summarise a truncated list as a complete one.
        "x-chainscope": {
            "writable": writable,
            "transport": "loopback only (127.0.0.1); token in an Authorization header",
            "contract": {
                "absence": (
                    "An empty result is never on its own a finding. Check "
                    "`reliable`, `unreachable_sources`, `complete`, `truncated` "
                    "and `filtered_out` before reporting nothing was found."
                ),
                "amounts": "Strings, wei-scale. Parsing as a JSON number rounds them.",
                "claims": (
                    "Every attribution carries source, confidence and rationale. "
                    "Reporting the label without them overstates it."
                ),
                "findings_vs_hypotheses": (
                    "Findings are observed; hypotheses are inferred and capped at "
                    "MEDIUM confidence. Do not merge them."
                ),
            },
            "mcp": "chainscope-mcp (same surface, as MCP tools)",
        },
    }
    return json.dumps(card, indent=2)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_DOC_CSS = """
:root { --bg:#0e0f13; --panel:#16181f; --line:#262a35; --fg:#e6e8ee;
        --muted:#8b90a0; --accent:#6ea8fe; --warn:#e0a458; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.65
  ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }
main { max-width:760px; margin:0 auto; padding:48px 24px 96px; }
h1 { font-size:26px; margin:0 0 8px; letter-spacing:-.01em; }
h2 { font-size:12px; letter-spacing:.12em; text-transform:uppercase;
  color:var(--muted); margin:44px 0 12px; font-weight:600; }
h3 { font-size:15px; margin:0 0 4px; }
p { margin:0 0 10px; }
.lede { color:#c3c8d6; }
.where { color:var(--muted); font-size:13px; }
.lim { color:var(--warn); }
section { border-left:2px solid var(--line); padding:2px 0 2px 14px;
  margin:0 0 20px; }
code { font-family:ui-monospace,Menlo,monospace; font-size:12.5px;
  background:var(--panel); padding:1px 5px; border-radius:4px; }
a { color:var(--accent); }
@media (prefers-color-scheme: light) {
  :root { --bg:#fbfbfd; --panel:#eef0f5; --line:#d8dce6; --fg:#14161c;
          --muted:#5b6172; --accent:#2a5db0; --warn:#8a5a12; }
  .lede { color:#3a4050; }
}
"""
