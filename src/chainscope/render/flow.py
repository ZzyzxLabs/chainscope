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
from typing import Any

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
        address = node.address.lower()
        if address not in depth:
            depth[address] = furthest + 1
    return depth


def _node_payload(node: Node, depth: int) -> dict[str, Any]:
    return {
        "id": node.address.lower(),
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
    }


def to_flow_html(graph: Graph, *, title: str = "chainscope flow") -> str:
    """Render ``graph`` as a self-contained layered flow page."""
    depth = layer_nodes(graph)
    nodes = [_node_payload(n, depth.get(n.address.lower(), 0)) for n in graph.nodes.values()]
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

    payload = {
        "title": title,
        "nodes": nodes,
        "edges": edges,
        "assets": list(assets.values()),
        "seeds": [s.split(":", 2)[-1].lower() if ":" in s else s.lower() for s in graph.seeds],
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
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <label>size by <select id="asset"></select></label>
  <button id="reset">clear route</button>
  <div class="legend" id="legend"></div>
  <div class="warn" id="warn" hidden></div>
</header>
<main><div id="wrap"><svg id="svg"></svg></div>
<aside><h2>selection</h2><div id="panel" class="muted">Click an address.</div></aside></main>
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
if (DATA.note) notes.push(DATA.note);
if (notes.length) { warn.textContent = notes.join(" "); warn.hidden = false; }

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
function draw(){
  const asset = sel.value;
  const edges = DATA.edges.filter(e => (e.asset || e.symbol || "native") === asset);
  const meta = DATA.assets.find(a => a.key === asset) || {max:"1", decimals:18, symbol:""};
  const maxRaw = BigInt(meta.max || "1") || 1n;

  // Only nodes this asset actually touches, plus the seeds, so switching asset
  // does not leave a field of disconnected boxes.
  const live = new Set(DATA.seeds);
  edges.forEach(e => { live.add(e.source); live.add(e.target); });
  const shown = DATA.nodes.filter(n => live.has(n.id));

  const cols = new Map();
  shown.forEach(n => {
    if (!cols.has(n.depth)) cols.set(n.depth, []);
    cols.get(n.depth).push(n); });
  const pos = new Map();
  [...cols.keys()].sort((a,b)=>a-b).forEach(d => {
    cols.get(d).sort((a,b) => a.display.localeCompare(b.display))
      .forEach((n,i) => pos.set(n.id, {x: PAD + d*COL, y: PAD + i*ROW}));
  });

  const width = PAD*2 + (Math.max(...[...cols.keys()], 0) + 1) * COL;
  const height = PAD*2 + Math.max(...[...cols.values()].map(c=>c.length), 1) * ROW;
  const svg = document.getElementById("svg");
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  svg.textContent = "";

  const g = document.createElementNS(NS,"g"); svg.appendChild(g);
  edges.forEach(e => {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return;
    const p = document.createElementNS(NS,"path");
    const x1 = a.x + 168, y1 = a.y + 14, x2 = b.x, y2 = b.y + 14, mx = (x1+x2)/2;
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
    const t = document.createElementNS(NS,"title");
    t.textContent = e.symbol + " " + fmt(e.raw, e.decimals) +
    "  (" + e.transfers + " transfer" + (e.transfers===1?"":"s") + ")";
    p.appendChild(t); g.appendChild(p);
  });

  shown.forEach(n => {
    const p = pos.get(n.id); if (!p) return;
    const grp = document.createElementNS(NS,"g");
    grp.setAttribute("class","node");
    grp.setAttribute("transform", `translate(${p.x},${p.y})`);
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
    tx.textContent = hedge +
      (n.display.length > 24 ? n.display.slice(0,23) + "\\u2026" : n.display);
    grp.appendChild(tx);
    const t = document.createElementNS(NS,"title");
    t.textContent = n.address + (n.label ? "  \\u2014 " + n.label : "") +
      (n.frontier ? "  (frontier: not expanded)" : "");
    grp.appendChild(t);
    grp.addEventListener("click",
      () => {
        selected = selected === n.id ? null : n.id;
        route = selected
          ? pathsTo(selected, DATA.edges.filter(
              x => (x.asset || x.symbol || "native") === sel.value))
          : {nodes: new Set(), edges: new Set(), hops: []};
        show(n); draw();
      });
    g.appendChild(grp);
  });
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
  out.push("</dl>");
  // Assembled from escaped parts only; no untrusted value reaches innerHTML raw.
  panel.innerHTML = out.join("");
}

sel.addEventListener("change", draw);
document.getElementById("reset").addEventListener("click", () => {
  selected = null; route = {nodes: new Set(), edges: new Set(), hops: []}; draw();
  const p = document.getElementById("panel");
  p.className="muted"; p.textContent="Click an address."; });
draw();
</script></body></html>
"""
