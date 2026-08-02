"""A layered fund-flow view: money left to right, hop by hop.

The force-directed graph in :mod:`chainscope.render.html` answers "who is
connected to whom". It is the wrong picture for the question an investigation
actually asks, which is **where did the money go**. A spring layout arranges
nodes by connectivity, so a five-hop laundering chain and a five-way split look
the same, and the reader has to reconstruct the sequence by following labels.

This lays the same graph out in columns by hop distance from the seed, so a
path reads as a path. That single change is most of what separates a
professional flow view from a network diagram.

Four things it does that a generic graph renderer will not:

**Edge width encodes amount, within one asset only.** Raw amounts are
comparable inside an asset and meaningless across them --- 1 USDC and 1 SHIB
are both "1". Each asset gets its own scale, and the legend says which asset is
being sized, because a single width over mixed assets is a number that means
nothing.

**The frontier is drawn differently from a leaf.** An address nobody expanded
and an address with nothing beyond it are the same shape in every generic
renderer, and telling them apart is the whole honesty of the underlying graph.
Frontier nodes are dashed and labelled; a diagram that hides the difference
silently overstates its own coverage.

**Truncation is on the canvas, not in a log.** If the walk stopped early the
banner says so, because a picture is read as a conclusion and nobody reads the
stderr that came with it.

**Attribution shows its confidence.** A HIGH claim and a SPECULATIVE one render
distinctly, so "Binance" and "maybe Binance" cannot be read as the same
statement --- which is exactly how a hedge becomes a fact three screenshots
later.

Self-contained: no CDN, no fonts, no network. It opens from a file:// URL on a
machine with no internet, which is where forensic work often happens.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any

from ..chains import address_key
from .graph import Edge, Graph, Node
from .html import _json_for_script

__all__ = ["layer_nodes", "to_flow_html"]

#: Category colours. Chosen for contrast in both themes rather than prettiness:
#: these are read next to each other and a reader must tell a mixer from an
#: exchange at a glance.
_PALETTE: dict[str, str] = {
    "cex": "#3b82f6",
    "dex": "#8b5cf6",
    "bridge": "#06b6d4",
    "mixer": "#ef4444",
    "sanctioned": "#dc2626",
    "illicit": "#f97316",
    "service": "#64748b",
    "contract": "#0ea5e9",
    "token": "#14b8a6",
    "": "#94a3b8",
}


def layer_nodes(graph: Graph) -> dict[str, int]:
    """Hop distance from the nearest seed, for every node.

    Breadth-first over directed edges, so a column is "how many hops the money
    travelled", not "how far apart these are in the drawing".

    Nodes unreachable from any seed get the last column rather than being
    dropped. They are usually inbound counterparties of a seed --- money coming
    *in* --- and a flow view that silently omits them shows an address that only
    ever sent.
    """
    seeds = [s.split(":", 2)[-1].lower() if ":" in s else s.lower() for s in graph.seeds]
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges.values():
        outgoing[edge.source.lower()].append(edge.target.lower())

    depth: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()
    for seed in seeds:
        depth[seed] = 0
        queue.append((seed, 0))

    while queue:
        address, level = queue.popleft()
        for nxt in outgoing.get(address, ()):
            if nxt not in depth:
                depth[nxt] = level + 1
                queue.append((nxt, level + 1))

    furthest = max(depth.values(), default=0)
    for node in graph.nodes.values():
        address = address_key(node.chain, node.address)
        if address not in depth:
            depth[address] = furthest + 1
    return depth


def _node_payload(node: Node, depth: int, visible_depth: int) -> dict[str, Any]:
    return {
        # Shipped but not drawn until its parent is expanded. Distinct from
        # `frontier`, which means nobody looked: this one was looked at and is
        # merely folded away.
        "collapsed": depth > visible_depth,
        # Keyed the way the node's own chain compares. Lowercasing merged
        # two distinct base58 addresses into one node on the canvas.
        "id": address_key(node.chain, node.address),
        "address": node.address,
        "display": node.display,
        "label": node.label,
        "category": node.category or "",
        "confidence": node.confidence,
        "source": node.source,
        "depth": depth,
        "seed": node.is_seed,
        # The distinction the whole graph exists to preserve.
        "frontier": node.is_frontier,
        "tags": list(node.tags),
    }


def _edge_payload(edge: Edge) -> dict[str, Any]:
    return {
        "source": edge.source.lower(),
        "target": edge.target.lower(),
        "asset": edge.asset or "",
        "symbol": edge.symbol or "",
        "decimals": edge.decimals,
        # Sent as a string: these routinely exceed 2^53 and JavaScript numbers
        # would round them. Formatting happens with BigInt on the page.
        "raw": str(edge.total_raw),
        "transfers": edge.transfer_count,
        # Unix seconds. An edge is an aggregate over a span, not a moment, so
        # both ends travel: scrubbing to a point in the middle of a span has to
        # be able to say the flow had started and not finished.
        "first": edge.first_seen,
        "last": edge.last_seen,
    }


def to_flow_html(
    graph: Graph,
    *,
    title: str = "chainscope flow",
    visible_depth: int | None = None,
    notes: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    """Render ``graph`` as a self-contained layered flow page.

    ``notes`` attaches case-log entries to addresses, keyed by lowercase
    address. They are shown with their author, because a note on a canvas with
    no name against it is a scratchpad and this is a record two people share.

    ``visible_depth`` collapses everything past that hop count. The rows still
    ship inside the page --- a file:// document cannot fetch --- so expanding is
    instant and works offline, which is where this kind of work often happens.

    What it is not: unlimited. Expanding reveals what was walked and no more,
    and the outermost ring stays frontier however many times it is clicked. A
    control that quietly stopped producing new nodes would read as "the money
    ends here", which is the one thing this view exists to prevent.
    """
    depth = layer_nodes(graph)
    cut = visible_depth if visible_depth is not None else max(depth.values(), default=0)
    nodes = [
        _node_payload(n, depth.get(address_key(n.chain, n.address), 0), cut)
        for n in graph.nodes.values()
    ]
    edges = [_edge_payload(e) for e in graph.edges.values()]

    assets: dict[str, dict[str, Any]] = {}
    for edge in graph.edges.values():
        key = edge.asset or edge.symbol or "native"
        entry = assets.setdefault(
            key,
            {"key": key, "symbol": edge.symbol or "?", "decimals": edge.decimals, "max": "0"},
        )
        if int(edge.total_raw) > int(entry["max"]):
            entry["max"] = str(edge.total_raw)

    stamps = [s for e in graph.edges.values() for s in (e.first_seen, e.last_seen) if s]
    payload = {
        "title": title,
        "t_min": min(stamps) if stamps else None,
        "t_max": max(stamps) if stamps else None,
        # How many edges carry no timestamp at all. They are shown at every
        # position rather than hidden, because a provider that omitted a
        # timestamp is not evidence the flow happened outside the window.
        "undated": sum(1 for e in graph.edges.values() if not e.first_seen),
        "nodes": nodes,
        "edges": edges,
        "notes": {k: list(v) for k, v in (notes or {}).items()},
        "assets": list(assets.values()),
        "seeds": [s.split(":", 2)[-1].lower() if ":" in s else s.lower() for s in graph.seeds],
        "visible_depth": cut,
        "collapsed_nodes": sum(1 for n in nodes if n["collapsed"]),
        "truncated": bool(graph.truncated),
        "note": graph.note or "",
        "frontier": len(graph.frontier()),
        "columns": max(depth.values(), default=0) + 1,
    }
    return (
        _PAGE.replace("__TITLE__", _escape(title))
        .replace("__DATA__", _json_for_script(payload))
        .replace("__PALETTE__", _json_for_script(_PALETTE))
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#0f172a;--muted:#64748b;--line:#cbd5e1;--panel:#f8fafc;--edge:#94a3b8}
@media (prefers-color-scheme:dark){
:root{--bg:#0b1120;--fg:#e2e8f0;--muted:#94a3b8;--line:#334155;--panel:#111827;--edge:#475569}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
background:var(--bg);color:var(--fg)}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}
.warn{background:#fef3c7;color:#78350f;border:1px solid #f59e0b;border-radius:6px;
padding:6px 10px;font-size:12.5px}
@media (prefers-color-scheme:dark){.warn{background:#422006;color:#fde68a}}
select,button{font:inherit;background:var(--panel);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:5px 9px}
main{display:flex;height:calc(100vh - 58px)}
#wrap{flex:1;overflow:auto}
svg{display:block}
aside{width:320px;border-left:1px solid var(--line);padding:14px 16px;overflow:auto;
background:var(--panel)}
code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;
background:var(--bg);border:1px solid var(--line);border-radius:4px;
padding:4px 6px;display:block;white-space:pre-wrap;word-break:break-all}
aside h2{font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted)}
dt{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
margin-top:10px}
dd{margin:2px 0 0;word-break:break-all;font-family:ui-monospace,SFMono-Regular,monospace;
font-size:12px}
.chip{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
border:1px solid var(--line)}
.legend{display:flex;gap:10px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.legend i{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:4px}
.node rect{cursor:pointer}
.node text{pointer-events:none}
.muted{color:var(--muted)}
#roster{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:12px}
#rosterlist{max-height:34vh;overflow:auto;margin:6px 0 0}
#rosterlist label{display:flex;gap:7px;align-items:center;padding:2px 0;
font-size:12px;cursor:pointer}
#rosterlist label span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#find{width:100%;font:inherit;font-size:12px;padding:4px 7px;border-radius:6px;
border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.rowbtn{font-size:11px;padding:2px 7px;margin-right:5px}
.mine{border-left:3px solid #a855f7;padding-left:7px}
.note{border-left:2px solid var(--line);padding-left:9px;margin:8px 0;font-size:12px}
.note b{font-weight:600}
.note .by{color:var(--muted);font-size:11px}
.gone{text-decoration:line-through;color:var(--muted)}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <label>size by <select id="asset"></select></label>
  <button id="reset">clear route</button>
  <button id="save" title="download this canvas as JSON">save canvas</button>
  <button id="load" title="restore a saved canvas">load</button>
  <input id="file" type="file" accept="application/json" hidden>
  <label id="scrubwrap" hidden>up to <input id="scrub" type="range" min="0" max="1000"
    value="1000" style="vertical-align:middle"> <span id="scrublabel"></span></label>
  <div class="legend" id="legend"></div>
  <span class="muted" style="font-size:11.5px">press ? for keys</span>
  <div class="warn" id="warn" hidden></div>
</header>
<main><div id="wrap"><svg id="svg"></svg></div>
<aside>
<section id="roster">
  <h2>on this canvas <span id="rostercount" class="muted"></span></h2>
  <input id="find" placeholder="filter by label or address">
  <div id="rosterlist"></div>
</section>
<h2>selection</h2><div id="panel" class="muted">Click an address for its
routes, or a flow for what it is made of.</div></aside></main>
<script>
const DATA = __DATA__, PALETTE = __PALETTE__;
const NS = "http://www.w3.org/2000/svg";
const COL = 260, ROW = 46, PAD = 40;

const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const sel = document.getElementById("asset");
DATA.assets.forEach(a => {
  const o = document.createElement("option");
  o.value = a.key; o.textContent = a.symbol; sel.appendChild(o);
});

const warn = document.getElementById("warn");
const notes = [];
if (DATA.truncated) notes.push(
  "The walk stopped at a limit. Addresses beyond it exist and are not drawn.");
if (DATA.frontier) notes.push(DATA.frontier +
  " dashed node(s) were seen but never expanded \\u2014 nobody looked past them.");
if (DATA.collapsed_nodes) notes.push(DATA.collapsed_nodes +
  " node(s) past hop " + DATA.visible_depth +
  " are folded. A node showing +n has that many counterparties in this file, " +
  "not drawn until clicked.");
if (DATA.note) notes.push(DATA.note);
function render(){
  warn.textContent = notes.join("  ");
  warn.hidden = notes.length === 0;
}
render();

const cats = [...new Set(DATA.nodes.map(n => n.category).filter(Boolean))];
document.getElementById("legend").innerHTML = cats.map(c =>
  '<span><i style="background:' + (PALETTE[c] || PALETTE[""]) + '"></i>' + esc(c) + '</span>'
).join("")
  + '<span><i style="border:1px dashed var(--edge);background:none"></i>frontier</span>';

function shortId(a){ return a.length > 14 ? a.slice(0,8) + "\u2026" + a.slice(-4) : a; }
function esc(s){ const d = document.createElement("div");
  d.textContent = s == null ? "" : s; return d.innerHTML; }

function fmt(raw, decimals){
  // BigInt: these routinely exceed 2^53 and Number would round them silently.
  let v; try { v = BigInt(raw); } catch { return String(raw); }
  if (decimals <= 0) return v.toLocaleString();
  const base = 10n ** BigInt(decimals);
  const whole = v / base, frac = v % base;
  if (frac === 0n) return whole.toLocaleString();
  const s = frac.toString().padStart(decimals, "0").slice(0, 4).replace(/0+$/, "");
  return whole.toLocaleString() + (s ? "." + s : "");
}

let selected = null;
// Vertical offsets from dragging. Kept out of the layout so a redraw --- an
// asset switch, a scrub --- keeps what the reader arranged.
const nudge = new Map();

// --------------------------------------------------------------- the canvas
//
// What makes this a document rather than a rendering. Everything a person did
// to the picture --- what they hid, what they opened, what they dragged, what
// they renamed --- keyed by **address**, never by position in the arrays.
//
// That is the whole property: re-run `chainscope graph` at a greater depth,
// load the saved canvas into the new page, and the work survives. Keyed by
// index it would silently reattach somebody's note to a different address,
// which is worse than losing it.
const KEY = "chainscope:canvas:" + DATA.seeds.join(",");
const CANVAS_VERSION = 1;
const hidden = new Set();
const renamed = new Map();

function canvasState(){
  return {
    version: CANVAS_VERSION,
    seeds: DATA.seeds,
    hidden: [...hidden],
    opened: [...opened],
    renamed: [...renamed],
    nudge: [...nudge],
    asset: sel.value,
  };
}

// State for an address this view does not contain is **kept**, not dropped. A
// narrower depth is a different question about the same case, and discarding
// somebody's work because a limit changed is the failure this project exists
// to avoid --- so it stays dormant and is counted out loud below.
let dormant = 0;

function applyState(raw){
  if (!raw || raw.version !== CANVAS_VERSION) return false;
  const here = new Set(DATA.nodes.map(n => n.id));
  dormant = 0;
  const take = (list, add) => (list || []).forEach(v => {
    const id = Array.isArray(v) ? v[0] : v;
    if (here.has(id)) add(v); else dormant++;
  });
  hidden.clear(); opened.clear(); renamed.clear(); nudge.clear();
  take(raw.hidden, id => hidden.add(id));
  take(raw.opened, id => opened.add(id));
  take(raw.renamed, ([id, text]) => renamed.set(id, text));
  take(raw.nudge, ([id, dy]) => nudge.set(id, dy));
  if (raw.asset && [...sel.options].some(o => o.value === raw.asset)) sel.value = raw.asset;
  return true;
}

function saveLocal(){
  try { localStorage.setItem(KEY, JSON.stringify(canvasState())); }
  catch (err) { /* private mode, or full. The canvas still works; it is the
                   convenience copy that is lost, and the explicit save is the
                   one that matters. */ }
}

function restoreLocal(){
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return applyState(JSON.parse(raw));
  } catch (err) { /* corrupt or unreadable: start clean rather than half-apply */ }
  return false;
}

// A person's own name for a node is not an attribution and must never look
// like one. It is drawn with a marker, and the panel says whose it is --- the
// same rule the type system applies to every claim, applied to the picture.
function displayOf(n){
  const mine = renamed.get(n.id);
  return mine ? "\u270e " + mine : n.display;
}
// Scrub position as a fraction of the case's span. An edge is an aggregate
// over a window, so "active by T" means it *started* by T -- an edge whose
// span straddles the cursor is shown, because the money had begun moving.
let cutoff = null;

function activeAt(e) {
  if (cutoff === null) return true;
  // No timestamp is not evidence the flow happened later. A provider that
  // omitted one leaves the edge visible at every position rather than hidden
  // at all of them, and the banner says how many are in that state.
  if (!e.first) return true;
  return e.first <= cutoff;
}

function stamp(t) {
  return new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
// Addresses the reader has opened. A node ships collapsed when it lies past
// the drawn depth; revealing it costs nothing because the rows are already
// here, and a file:// page cannot fetch anyway.
const opened = new Set();

function isShown(n) {
  if (!n.collapsed) return true;
  // Visible once something pointing at it has been opened.
  return DATA.edges.some(e => e.target === n.id && opened.has(e.source));
}

function hiddenBehind(id) {
  return DATA.edges.filter(e =>
    e.source === id && (byId.get(e.target) || {}).collapsed && !opened.has(id)).length;
}
let route = {nodes: new Set(), edges: new Set(), hops: []};

// Every path from a seed to the clicked address, not just the shortest.
//
// A laundering case is a tree of routes and reading one route at a time is the
// task. The shortest path alone would hide a split that rejoins -- which is the
// structure worth seeing, since somebody split the funds for a reason.
//
// Bounded: cycles are refused by the visited set, and the search stops at
// MAX_PATHS so a dense graph cannot hang the page. Hitting the cap is reported
// rather than silently truncating the answer.
const MAX_PATHS = 40;
function pathsTo(target, edges) {
  const out = [], nodes = new Set(), used = new Set();
  const next = new Map();
  edges.forEach(e => {
    if (!next.has(e.source)) next.set(e.source, []);
    next.get(e.source).push(e);
  });
  let capped = false;
  const walk = (at, trail, seen) => {
    if (out.length >= MAX_PATHS) { capped = true; return; }
    if (at === target) {
      out.push([...trail]);
      trail.forEach(e => { used.add(e.source + ">" + e.target + ">" + e.asset);
                           nodes.add(e.source); nodes.add(e.target); });
      nodes.add(target);
      return;
    }
    for (const e of next.get(at) || []) {
      if (seen.has(e.target)) continue;   // a cycle is not a route
      seen.add(e.target); trail.push(e);
      walk(e.target, trail, seen);
      trail.pop(); seen.delete(e.target);
    }
  };
  DATA.seeds.forEach(s => walk(s, [], new Set([s])));
  return {nodes, edges: used, hops: out, capped};
}
// What `draw` last put on the canvas. The roster is headed "on this canvas"
// and has to mean it: with the time scrub pulled back, most nodes are absent
// for a reason that is not hiding, and a roster reporting them as present is
// the same defect as a picture silently omitting them --- a count that does
// not match what is drawn.
let drawn = new Set();

const find = document.getElementById("find");
const rosterList = document.getElementById("rosterlist");
const rosterCount = document.getElementById("rostercount");

function roster(){
  const q = find.value.trim().toLowerCase();
  const rows = DATA.nodes
    .filter(n => !q || n.id.includes(q) || displayOf(n).toLowerCase().includes(q))
    .sort((a,b) => a.depth - b.depth || displayOf(a).localeCompare(displayOf(b)));
  rosterList.textContent = "";
  rows.forEach(n => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !hidden.has(n.id);
    box.addEventListener("change", () => {
      if (box.checked) hidden.delete(n.id); else hidden.add(n.id);
      saveLocal(); draw();
    });
    const text = document.createElement("span");
    const absent = !drawn.has(n.id) && !hidden.has(n.id);
    text.textContent = displayOf(n) + "  \u00b7 h" + n.depth +
      (absent ? "  \u00b7 not at this time/asset" : "");
    text.title = n.address;
    if (renamed.has(n.id)) text.className = "mine";
    if (hidden.has(n.id) || absent) text.classList.add("gone");
    label.appendChild(box); label.appendChild(text);
    rosterList.appendChild(label);
  });

  // Said out loud, always. A picture quietly missing nodes somebody hid is a
  // picture that looks complete and is not --- which is the single failure
  // this whole view is arranged against.
  const parts = [drawn.size + " drawn of " + DATA.nodes.length];
  if (hidden.size) parts.push(hidden.size + " hidden by you");
  // Distinct from hiding, and said separately: one is a choice somebody made
  // and the other is the question they asked. Collapsing them into "not shown"
  // would leave a reader unable to tell a filtered view from an edited one.
  const filtered = DATA.nodes.filter(n => !drawn.has(n.id) && !hidden.has(n.id)).length;
  if (filtered) parts.push(filtered + " outside the current time or asset");
  if (dormant) parts.push(dormant + " saved for addresses not in this view");
  rosterCount.textContent = "\u2014 " + parts.join(", ");
}

function draw(){
  const asset = sel.value;
  const edges = DATA.edges.filter(
    e => (e.asset || e.symbol || "native") === asset && activeAt(e));
  const meta = DATA.assets.find(a => a.key === asset) || {max:"1", decimals:18, symbol:""};
  const maxRaw = BigInt(meta.max || "1") || 1n;

  // Only nodes this asset actually touches, plus the seeds, so switching asset
  // does not leave a field of disconnected boxes.
  const live = new Set(DATA.seeds);
  edges.forEach(e => { live.add(e.source); live.add(e.target); });
  // Two separate questions, deliberately not merged. `isShown` is about
  // coverage --- has the walk revealed this node yet. `hidden` is a view
  // choice somebody made. Folding them into one predicate would make the fold
  // logic untestable on its own and would let a display preference read as a
  // fact about what was reached.
  const shown = DATA.nodes.filter(
    n => live.has(n.id) && isShown(n) && !hidden.has(n.id));
  const visible = new Set(shown.map(n => n.id));

  const cols = new Map();
  shown.forEach(n => {
    if (!cols.has(n.depth)) cols.set(n.depth, []);
    cols.get(n.depth).push(n); });
  const pos = new Map();
  [...cols.keys()].sort((a,b)=>a-b).forEach(d => {
    cols.get(d).sort((a,b) => displayOf(a).localeCompare(displayOf(b)))
      .forEach((n,i) => pos.set(n.id, {x: PAD + d*COL, y: PAD + i*ROW}));
  });

  const width = PAD*2 + (Math.max(...[...cols.keys()], 0) + 1) * COL;
  const height = PAD*2 + Math.max(...[...cols.values()].map(c=>c.length), 1) * ROW;
  const svg = document.getElementById("svg");
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  svg.textContent = "";

  const g = document.createElementNS(NS,"g"); svg.appendChild(g);
  edges.forEach(e => {
    if (!visible.has(e.source) || !visible.has(e.target)) return;
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return;
    const p = document.createElementNS(NS,"path");
    const x1 = a.x + 168, y1 = a.y + 14 + (nudge.get(e.source) || 0);
    const x2 = b.x, y2 = b.y + 14 + (nudge.get(e.target) || 0), mx = (x1+x2)/2;
    p.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
    // Width within one asset only. Log scale, because one large flow otherwise
    // renders every other edge as a hairline.
    const ratio = Number(BigInt(e.raw) * 1000n / maxRaw) / 1000;
    p.setAttribute("stroke-width", (1 + Math.log10(1 + ratio*9) * 5).toFixed(2));
    const key = e.source + ">" + e.target + ">" + e.asset;
    const onRoute = route.edges.has(key);
    const dim = selected && !onRoute;
    p.setAttribute("stroke", onRoute ? "#f59e0b" : "var(--edge)");
    p.setAttribute("stroke-opacity", dim ? 0.08 : (onRoute ? 0.95 : 0.55));
    p.setAttribute("fill","none");
    // A wide transparent stroke underneath: a hairline edge is unclickable,
    // and "I cannot hit the thing I want to inspect" is the most common way a
    // graph view is abandoned.
    const hit = document.createElementNS(NS,"path");
    hit.setAttribute("d", p.getAttribute("d"));
    hit.setAttribute("stroke", "transparent");
    hit.setAttribute("stroke-width", "12");
    hit.setAttribute("fill", "none");
    hit.style.cursor = "pointer";
    hit.addEventListener("click", ev => { ev.stopPropagation(); showEdge(e); });
    g.appendChild(hit);
    const t = document.createElementNS(NS,"title");
    t.textContent = e.symbol + " " + fmt(e.raw, e.decimals) +
    "  (" + e.transfers + " transfer" + (e.transfers===1?"":"s") + ")";
    p.appendChild(t); g.appendChild(p);
  });

  shown.forEach(n => {
    const p = pos.get(n.id); if (!p) return;
    const grp = document.createElementNS(NS,"g");
    grp.setAttribute("class","node");
    grp.setAttribute("transform", `translate(${p.x},${p.y + (nudge.get(n.id) || 0)})`);
    const r = document.createElementNS(NS,"rect");
    r.setAttribute("width",168); r.setAttribute("height",28); r.setAttribute("rx",5);
    r.setAttribute("fill", PALETTE[n.category] || PALETTE[""]);
    const lit = !selected || route.nodes.has(n.id);
    r.setAttribute("fill-opacity", lit ? 0.85 : 0.15);
    if (n.frontier){
      r.setAttribute("stroke-dasharray","4 3");
      r.setAttribute("fill-opacity",0.18); }
    r.setAttribute("stroke", n.seed ? "#f59e0b" : "var(--line)");
    r.setAttribute("stroke-width", n.seed ? 2.5 : 1);
    grp.appendChild(r);
    const tx = document.createElementNS(NS,"text");
    tx.setAttribute("x",9); tx.setAttribute("y",18);
    tx.setAttribute("font-size","12"); tx.setAttribute("fill","#fff");
    // A claim below HIGH is rendered as a claim. "Binance" and "maybe Binance"
    // must not read as the same statement.
    const hedge = n.label && n.confidence >= 0 && n.confidence < 3 ? "? " : "";
    const shownName = displayOf(n);
    tx.textContent = hedge +
      (shownName.length > 24 ? shownName.slice(0,23) + "\\u2026" : shownName);
    grp.appendChild(tx);
    const t = document.createElementNS(NS,"title");
    t.textContent = n.address + (n.label ? "  \\u2014 " + n.label : "") +
      (n.frontier ? "  (frontier: not expanded)" : "");
    grp.appendChild(t);
    // Dragging moves the box, never the column. Position along x encodes hop
    // distance from the seed, so letting a node slide between columns would
    // let somebody rearrange the picture into a claim the data does not make.
    let dragging = false, moved = false, oy = 0, base = 0;
    grp.addEventListener("mousedown", ev => {
      dragging = true; moved = false; oy = ev.clientY;
      base = nudge.get(n.id) || 0;
      ev.preventDefault();
    });
    window.addEventListener("mousemove", ev => {
      if (!dragging) return;
      const dy = ev.clientY - oy;
      if (Math.abs(dy) > 3) moved = true;
      nudge.set(n.id, base + dy);
      grp.setAttribute("transform", `translate(${p.x},${p.y + base + dy})`);
    });
    window.addEventListener("mouseup", () => {
      if (dragging && moved) draw();
      dragging = false;
    });
    grp.addEventListener("click",
      () => {
        if (hiddenBehind(n.id)) { opened.add(n.id); show(n); draw(); return; }
        selected = selected === n.id ? null : n.id;
        route = selected
          ? pathsTo(selected, DATA.edges.filter(
              x => (x.asset || x.symbol || "native") === sel.value))
          : {nodes: new Set(), edges: new Set(), hops: []};
        show(n); draw();
      });
    g.appendChild(grp);
  });

  drawn = visible;
  roster();
}

function showEdge(e) {
  const panel = document.getElementById("panel");
  panel.className = "";
  const out = ["<dl>"];
  out.push("<dt>flow</dt><dd>" + esc(shortId(e.source)) + " \u2192 " +
    esc(shortId(e.target)) + "</dd>");
  out.push("<dt>total</dt><dd>" + esc(fmt(e.raw, e.decimals) + " " + (e.symbol || "")) +
    "</dd>");
  // An aggregate over n transfers, said plainly. A reader who takes this for a
  // single payment draws the wrong conclusion about size and timing at once.
  out.push("<dt>transfers</dt><dd>" + e.transfers +
    (e.transfers === 1 ? "" : " (this is their sum, not one payment)") + "</dd>");
  if (e.first) out.push("<dt>first</dt><dd>" + esc(stamp(e.first)) + "</dd>");
  if (e.last && e.last !== e.first)
    out.push("<dt>last</dt><dd>" + esc(stamp(e.last)) + "</dd>");
  if (e.asset) out.push("<dt>asset</dt><dd>" + esc(e.asset) + "</dd>");
  out.push("</dl>");
  // The individual transfers are not in this file --- a real case has more
  // than a page can hold. The query that gets them is, so the panel ends in
  // something runnable rather than a dead end.
  const where = "sender = '" + e.source + "' AND recipient = '" + e.target + "'" +
    (e.asset ? " AND asset = '" + e.asset + "'" : " AND asset IS NULL");
  out.push('<dt>the individual transfers</dt><dd class="muted">not in this file. Run:</dd>');
  out.push('<dd><code>chainscope sql "SELECT tx_hash, amount_raw, block, ' +
    'timestamp FROM transfers WHERE ' + esc(where) + ' ORDER BY block"</code></dd>');
  panel.innerHTML = out.join("");
}

const CONF = ["speculative","low","medium","high","certain"];
function show(n){
  const panel = document.getElementById("panel");
  panel.className = "";
  const out = [];
  out.push("<dl>");
  out.push("<dt>address</dt><dd>" + esc(n.address) + "</dd>");
  if (n.label){
    const c = n.confidence >= 0 && n.confidence < CONF.length ? CONF[n.confidence] : "unstated";
    out.push("<dt>label</dt><dd>" + esc(n.label) +
      ' <span class="chip">' + esc(c) + "</span></dd>");
    if (n.confidence >= 0 && n.confidence < 3)
      out.push('<dd class="muted">Below HIGH: this is a claim, not an identification.</dd>');
  } else {
    out.push('<dt>label</dt><dd class="muted">none \\u2014 unlabelled, not unimportant</dd>');
  }
  if (n.source) out.push("<dt>source</dt><dd>" + esc(n.source) + "</dd>");
  if (n.category) out.push("<dt>category</dt><dd>" + esc(n.category) + "</dd>");
  out.push("<dt>hop</dt><dd>" + n.depth + " from seed</dd>");
  if (route.hops.length) {
    out.push("<dt>routes from seed</dt><dd>" + route.hops.length +
      (route.capped ? " (capped; there are more)" : "") + "</dd>");
    // Every route, not the shortest one. A split that rejoins is the structure
    // worth seeing and a single path would hide it.
    route.hops.slice(0, 6).forEach(h => {
      const step = h.map(e => shortId(e.target)).join(" \u2192 ");
      out.push('<dd class="muted">' + esc(shortId(h[0].source) + " \u2192 " + step) + "</dd>");
    });
  } else if (selected === n.id && !n.seed) {
    out.push('<dt>routes from seed</dt><dd class="muted">none in this asset \u2014 ' +
      'reached by a different asset, or only inbound</dd>');
  }
  if (n.frontier)
    out.push('<dt>coverage</dt><dd class="muted">Frontier. Its counterparties '
      + 'were never fetched \\u2014 the picture stops here because nobody looked, '
      + 'not because there is nothing.</dd>');
  if (n.tags.length) out.push("<dt>tags</dt><dd>" + n.tags.map(esc).join(", ") + "</dd>");
  const mine = renamed.get(n.id);
  if (mine) {
    // Marked as yours every time it is shown. A name somebody typed and a
    // sourced attribution must never read as the same statement --- that
    // confusion is what `Attribution` exists to prevent, and a canvas that
    // allowed it here would undo the guarantee at the last step.
    out.push('<dt>your name for it</dt><dd class="mine">' + esc(mine) +
      '</dd><dd class="muted">Yours, not an attribution. It is not stored as a ' +
      'claim and travels only in this canvas. To assert it, run ' +
      '<code>chainscope tag</code> with a source.</dd>');
  }
  (DATA.notes[n.id] || []).forEach(note => {
    out.push('<dd class="note' + (note.superseded ? " gone" : "") + '"><b>' +
      esc(note.kind) + "</b> " + esc(note.body) +
      '<div class="by">' + esc(note.by) + " \u00b7 " + esc(note.at) +
      (note.superseded ? " \u00b7 superseded" : "") + "</div></dd>");
  });
  out.push("</dl>");
  // Assembled from escaped parts only; no untrusted value reaches innerHTML raw.
  panel.innerHTML = out.join("");

  const bar = document.createElement("div");
  bar.style.marginTop = "12px";
  const rename = document.createElement("button");
  rename.className = "rowbtn";
  rename.textContent = mine ? "rename" : "name it";
  rename.addEventListener("click", () => {
    const next = window.prompt(
      "Your name for this node. It is not an attribution and is not stored as " +
      "one --- to assert what this address is, use `chainscope tag`.",
      mine || "");
    if (next === null) return;
    if (next.trim()) renamed.set(n.id, next.trim()); else renamed.delete(n.id);
    saveLocal(); draw(); show(n);
  });
  bar.appendChild(rename);
  const hide = document.createElement("button");
  hide.className = "rowbtn";
  hide.textContent = "hide from canvas";
  hide.addEventListener("click", () => {
    hidden.add(n.id); saveLocal(); draw();
  });
  bar.appendChild(hide);
  panel.appendChild(bar);
}

if (DATA.t_min !== null && DATA.t_max !== null && DATA.t_max > DATA.t_min) {
  const wrap = document.getElementById("scrubwrap");
  const bar = document.getElementById("scrub");
  const label = document.getElementById("scrublabel");
  wrap.hidden = false;
  const setCut = () => {
    const f = Number(bar.value) / 1000;
    cutoff = f >= 1 ? null : Math.round(DATA.t_min + (DATA.t_max - DATA.t_min) * f);
    label.textContent = cutoff === null ? "all of it" : stamp(cutoff);
    // Selection is cleared: a highlighted route through an edge that is no
    // longer shown would leave a lit path with a hole in it.
    selected = null; route = {nodes: new Set(), edges: new Set(), hops: []};
    draw();
  };
  bar.addEventListener("input", setCut);
  setCut();
}
if (DATA.undated) notes.push(DATA.undated +
  " flow(s) carry no timestamp and stay visible at every scrub position \u2014 " +
  "a provider omitting one is not evidence the money moved later.");
sel.addEventListener("change", draw);
// Keyboard. Every one of these is something the mouse can already do --- the
// point is not new capability but not having to leave the picture to get it.
const HELP = [
  ["Esc", "clear the route and the panel"],
  ["a", "cycle the asset being sized"],
  ["e", "expand every folded node"],
  ["r", "reset dragged positions"],
  ["?", "this list"],
];
window.addEventListener("keydown", ev => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  if (ev.key === "Escape") { document.getElementById("reset").click(); }
  else if (ev.key === "a") { sel.selectedIndex = (sel.selectedIndex + 1) % sel.length;
                             sel.dispatchEvent(new Event("change")); }
  else if (ev.key === "e") { DATA.nodes.forEach(n => opened.add(n.id)); draw(); }
  else if (ev.key === "r") { nudge.clear(); draw(); }
  else if (ev.key === "?") {
    const panel = document.getElementById("panel");
    panel.className = "";
    panel.innerHTML = "<dl>" + HELP.map(([k, what]) =>
      "<dt>" + esc(k) + "</dt><dd>" + esc(what) + "</dd>").join("") + "</dl>";
  }
});

document.getElementById("reset").addEventListener("click", () => {
  selected = null; opened.clear();
  route = {nodes: new Set(), edges: new Set(), hops: []};
  saveLocal(); draw();
  const p = document.getElementById("panel");
  p.className="muted"; p.textContent="Click an address."; });

// Two kinds of saving, and they are for different things. localStorage is the
// one nobody should have to think about --- it survives a refresh and a closed
// tab. The file is the one that leaves this machine: it goes next to the case,
// into version control, or to a colleague, and it is the only one that can.
document.getElementById("save").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(canvasState(), null, 2)],
    {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (DATA.title || "canvas").replace(/[^a-z0-9._-]+/gi, "-") + ".canvas.json";
  a.click();
  URL.revokeObjectURL(a.href);
});
const fileInput = document.getElementById("file");
document.getElementById("load").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    let parsed;
    try { parsed = JSON.parse(String(reader.result)); }
    catch (err) { notes.push("that file is not readable JSON"); render(); return; }
    if (!applyState(parsed)) {
      notes.push("that canvas was written by a different version and was not applied");
    } else if (dormant) {
      // Not silent. A canvas that quietly dropped a third of somebody's work
      // would look like it restored cleanly.
      notes.push(dormant + " saved item(s) refer to addresses this graph does " +
        "not contain. They are kept and will apply again if you re-run the " +
        "graph wider.");
    }
    render();
    saveLocal(); draw();
  };
  reader.readAsText(file);
  fileInput.value = "";
});

restoreLocal();
if (dormant) notes.push(dormant + " item(s) in the restored canvas refer to " +
  "addresses outside this view --- kept, not discarded.");
render();
draw();
</script></body></html>
"""
