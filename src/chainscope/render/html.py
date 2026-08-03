"""A self-contained fund-flow view.

One HTML file with the data inlined and the layout code alongside it. No server,
no build step, no CDN --- open it from disk, or attach it to a case bundle and
have it still work on a machine that has never heard of this project, five years
from now. Every tool that renders a graph by fetching a library at load time
produces artefacts that stop working, and an investigation exhibit that stops
working is worthless precisely when it matters.

The layout is a small force simulation written here rather than pulled in. That
is a real trade --- d3-force is better --- but a graph of a few hundred
aggregated edges does not need better, and self-containment does not survive a
dependency.

**What this shows that a generic graph viewer does not:**

*Confidence, on the node.* A label rendered without it invites the reader to
treat MEDIUM as fact. Nodes carry a confidence badge, and unlabelled addresses
stay visibly unlabelled rather than being given their own truncated hex as a
name.

*The frontier, drawn differently.* Dashed outlines mark addresses that were seen
but never expanded. Without that, a diagram that stopped because nobody looked
further is indistinguishable from one that stopped because there was nothing
further --- and the reader will assume the second.

*Exact amounts.* Values arrive as strings and are formatted with integer
arithmetic in the page. A JSON number is a double, and 10 ETH already exceeds
what one holds exactly.
"""

from __future__ import annotations

import html
import json
from typing import Any

from .graph import Graph

__all__ = ["to_html"]

# Also used by the dashboard: the same <script> escaping problem, and one
# implementation is one place to get it right.


def _json_for_script(value: Any) -> str:
    """Serialise JSON safe to embed inside a ``<script>`` element.

    ``json.dumps`` alone is not. An HTML parser looks for the literal ``</script``
    while scanning script content and does not care that it sits inside a JSON
    string, so a label containing ``</script><img src=x onerror=…>`` closes the
    block and injects markup. Labels arrive from imported files and from agents:
    this is untrusted text by construction.

    Escaping the angle brackets as ``\\u003c``/``\\u003e`` is enough --- the JSON
    decoder produces the same string, and no byte sequence the parser reacts to
    survives. ``&`` goes too, so nothing can be smuggled through an HTML entity,
    and U+2028/U+2029 because they terminate a JavaScript string literal even
    though JSON permits them raw.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_CSS = """
:root {
  --bg: #fbfbfa; --fg: #16161a; --muted: #6b7280; --line: #d4d4d8;
  --panel: #ffffff; --shadow: 0 1px 3px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#131316; --fg:#e8e8ea; --muted:#9b9ba3; --line:#2e2e35;
          --panel:#1b1b20; --shadow: 0 1px 3px rgba(0,0,0,.4); }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }
header { padding:14px 20px; border-bottom:1px solid var(--line);
  display:flex; gap:20px; align-items:baseline; flex-wrap:wrap; }
h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:-.01em; }
.meta { color:var(--muted); font-size:12px; display:flex; gap:14px; flex-wrap:wrap; }
.warn { color:#b45309; font-weight:600; }
main { display:flex; height:calc(100vh - 56px); }
@media (max-width:760px){ main{flex-direction:column; height:auto;} #canvas{height:60vh;} }
#canvas { flex:1; position:relative; overflow:hidden; }
svg { width:100%; height:100%; display:block; cursor:grab; }
svg:active { cursor:grabbing; }
aside { width:320px; border-left:1px solid var(--line); padding:16px;
  overflow-y:auto; background:var(--panel); }
@media (max-width:760px){ aside{width:auto; border-left:none;
  border-top:1px solid var(--line);} }
aside h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:0 0 10px; }
.row { display:flex; justify-content:space-between; gap:10px; padding:4px 0;
  border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
.row:last-child { border-bottom:none; }
.k { color:var(--muted); }
.v { text-align:right; word-break:break-all; }
.legend { display:flex; gap:12px; flex-wrap:wrap; margin-top:6px; font-size:12px;
  color:var(--muted); }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:4px; vertical-align:middle; }
.node-label { font-size:11px; pointer-events:none; fill:var(--fg); }
.hint { color:var(--muted); font-size:12px; margin-top:14px; }
code { background:rgba(127,127,127,.14); padding:1px 4px; border-radius:3px; }
"""

# Category colours, matched to the DOT exporter so a case looks the same
# whichever way it is rendered.
_PALETTE: dict[str, str] = {
    "sanctioned": "#c62828",
    "mixer": "#ad1457",
    "cex": "#1565c0",
    "dex": "#2e7d32",
    "bridge": "#6a1b9a",
    "illicit": "#e65100",
}

_JS = """
const DATA = __DATA__;
const PALETTE = __PALETTE__;
const svgNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("g");
const view = document.getElementById("view");

// Amounts arrive as strings because they exceed what a JSON number holds
// exactly. Formatting therefore works on the digits, never on a float.
// Four *significant* fraction digits, not four fraction digits. Slicing at a
// fixed position rendered one wei as "0.0000", which on a flow graph reads as
// nothing having moved --- and a dust amount is the whole signal in a peel
// chain or an address-poisoning transfer. Mirrors `_fmt` in dashboard.py;
// tests/unit/test_amount_formatting_agrees.py holds the two together.
function human(raw, decimals) {
  const neg = raw.startsWith("-");
  const digits = (neg ? raw.slice(1) : raw).padStart(decimals + 1, "0");
  const whole = digits.slice(0, digits.length - decimals) || "0";
  let frac = decimals ? digits.slice(digits.length - decimals) : "";
  frac = frac.replace(/0+$/, "");
  if (frac) {
    let keep = 4;
    if (/^0*$/.test(whole)) keep += frac.length - frac.replace(/^0+/, "").length;
    frac = frac.slice(0, keep);
  }
  const grouped = whole.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
  return (neg ? "-" : "") + grouped + (frac ? "." + frac : "");
}

const nodes = DATA.nodes.map(n => ({...n, x: 0, y: 0, vx: 0, vy: 0}));
const byId = new Map(nodes.map(n => [n.id, n]));
const links = DATA.links
  .map(l => ({...l, s: byId.get(l.source), t: byId.get(l.target)}))
  .filter(l => l.s && l.t);

// Seed on a circle rather than at random: a deterministic start means the same
// case renders the same way twice, which matters when two people are comparing
// screenshots of it.
const R = 220;
nodes.forEach((n, i) => {
  const a = (i / Math.max(1, nodes.length)) * Math.PI * 2;
  n.x = Math.cos(a) * R + (n.seed ? 0 : 0);
  n.y = Math.sin(a) * R;
  if (n.seed) { n.x = 0; n.y = 0; }
});

function step() {
  // Repulsion.
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy || 0.01;
      const f = 9000 / d2;
      const d = Math.sqrt(d2);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }
  }
  // Attraction along edges.
  for (const l of links) {
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - 120) * 0.012;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
  }
  for (const n of nodes) {
    if (n.seed) { n.vx = 0; n.vy = 0; continue; }  // anchor the seed
    n.x += (n.vx *= 0.82); n.y += (n.vy *= 0.82);
  }
}
for (let i = 0; i < 260; i++) step();

function colour(n) { return PALETTE[n.category] || "#94a3b8"; }

const defs = document.createElementNS(svgNS, "defs");
defs.innerHTML =
  '<marker id="arrow" viewBox="0 0 10 10" refX="20" refY="5" markerWidth="6" ' +
  'markerHeight="6" orient="auto-start-reverse">' +
  '<path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker>';
svg.appendChild(defs);

for (const l of links) {
  const line = document.createElementNS(svgNS, "line");
  line.setAttribute("x1", l.s.x); line.setAttribute("y1", l.s.y);
  line.setAttribute("x2", l.t.x); line.setAttribute("y2", l.t.y);
  line.setAttribute("stroke", "#94a3b8");
  line.setAttribute("stroke-width", Math.min(4, 1 + Math.log10(l.transfers + 1) * 2));
  line.setAttribute("marker-end", "url(#arrow)");
  line.setAttribute("opacity", ".6");
  const title = document.createElementNS(svgNS, "title");
  title.textContent = human(l.total_raw, l.decimals) + " " + l.symbol +
    "  (" + l.transfers + " transfer" + (l.transfers === 1 ? "" : "s") + ")";
  line.appendChild(title);
  view.appendChild(line);
}

for (const n of nodes) {
  const g = document.createElementNS(svgNS, "g");
  g.setAttribute("transform", `translate(${n.x},${n.y})`);
  const c = document.createElementNS(svgNS, "circle");
  c.setAttribute("r", n.seed ? 13 : 9);
  c.setAttribute("fill", colour(n));
  c.setAttribute("stroke", "var(--fg)");
  c.setAttribute("stroke-width", n.seed ? 2.5 : 1);
  // Dashed means seen but never expanded. Without the distinction, "we stopped
  // here" and "there is nothing further" look identical.
  if (n.frontier) c.setAttribute("stroke-dasharray", "3,2");
  const title = document.createElementNS(svgNS, "title");
  title.textContent = n.address + "\\n" + (n.label || "(unlabelled)") +
    (n.category ? "  [" + n.category + "]" : "") +
    (n.confidence >= 0 ? "\\nconfidence: " + n.confidence + "/4" : "\\nno attribution") +
    (n.source ? "\\nsource: " + n.source : "") +
    (n.frontier ? "\\nFRONTIER - not expanded" : "");
  c.appendChild(title);
  g.appendChild(c);

  const label = document.createElementNS(svgNS, "text");
  label.setAttribute("class", "node-label");
  label.setAttribute("x", 13); label.setAttribute("y", 4);
  label.textContent = n.display;
  g.appendChild(label);

  g.style.cursor = "pointer";
  g.addEventListener("click", () => select(n));
  view.appendChild(g);
}

function select(n) {
  const rows = [
    ["address", n.address],
    ["chain", n.chain],
    ["label", n.label || "(unlabelled)"],
    ["category", n.category || "-"],
    ["confidence", n.confidence >= 0 ? n.confidence + " / 4" : "no attribution"],
    ["source", n.source || "-"],
    ["expanded", n.expanded ? "yes" : "no - frontier"],
  ];
  // Built with textContent rather than innerHTML. These values are labels and
  // sources, which come from imported files and from agents; interpolating them
  // into markup is the same injection by a second route.
  const panel = document.getElementById("detail");
  panel.textContent = "";
  for (const [k, v] of rows) {
    const row = document.createElement("div");
    row.className = "row";
    const key = document.createElement("span");
    key.className = "k"; key.textContent = k;
    const val = document.createElement("span");
    val.className = "v"; val.textContent = String(v);
    row.append(key, val);
    panel.append(row);
  }
}
if (nodes.length) select(nodes.find(n => n.seed) || nodes[0]);

// Pan and zoom.
let tx = 0, ty = 0, scale = 1, dragging = false, lx = 0, ly = 0;
function apply() { view.setAttribute("transform",
  `translate(${tx},${ty}) scale(${scale})`); }
function fit() {
  const box = view.getBBox(), r = svg.getBoundingClientRect();
  scale = Math.min(r.width / (box.width + 120), r.height / (box.height + 120), 1.6);
  tx = r.width / 2 - (box.x + box.width / 2) * scale;
  ty = r.height / 2 - (box.y + box.height / 2) * scale;
  apply();
}
svg.addEventListener("mousedown", e => { dragging = true; lx = e.clientX; ly = e.clientY; });
addEventListener("mouseup", () => dragging = false);
addEventListener("mousemove", e => {
  if (!dragging) return;
  tx += e.clientX - lx; ty += e.clientY - ly; lx = e.clientX; ly = e.clientY; apply();
});
svg.addEventListener("wheel", e => {
  e.preventDefault();
  const k = e.deltaY < 0 ? 1.1 : 1 / 1.1;
  scale *= k; tx = e.offsetX - (e.offsetX - tx) * k; ty = e.offsetY - (e.offsetY - ty) * k;
  apply();
}, {passive: false});
addEventListener("resize", fit);
fit();
"""


def to_html(graph: Graph, *, title: str = "chainscope") -> str:
    """Render a graph as one self-contained HTML document.

    Nothing is fetched at load time. The result opens from a file path, from a
    case bundle, or from a USB stick on a machine with no network.
    """
    payload: dict[str, Any] = {
        "nodes": [n.to_dict() for n in graph.nodes.values()],
        "links": [e.to_dict() for e in graph.edges.values()],
        "summary": graph.summary(),
    }
    summary = payload["summary"]

    totals = graph.totals_by_asset()
    # Per asset, never combined: two assets summed into one number is a figure
    # denominated in nothing.
    total_rows = "".join(
        f'<div class="row"><span class="k">{html.escape(sym or "native")}</span>'
        f'<span class="v">{raw}</span></div>'
        for sym, raw in sorted(totals.items())
    )

    legend = "".join(
        f'<span><i style="background:{colour}"></i>{html.escape(name)}</span>'
        for name, colour in _PALETTE.items()
    )

    warning = ""
    if summary["truncated"]:
        warning = (
            '<span class="warn">TRUNCATED &mdash; a limit stopped this walk; '
            "the graph is not the whole case</span>"
        )

    # Palette first, then the data. `str.replace` does not re-scan what it
    # inserted, so whichever goes last cannot be affected by the other --- but
    # the data used to go first, and a node label of `__PALETTE__` was then
    # still present when the palette substitution ran, replacing an address's
    # label with the palette JSON. Labels come from imported CSVs, so the value
    # is not ours.
    script = _JS.replace("__PALETTE__", _json_for_script(_PALETTE)).replace(
        "__DATA__", _json_for_script(payload)
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    <span>{summary["nodes"]} addresses</span>
    <span>{summary["edges"]} flows</span>
    <span>{summary["transfers"]} transfers</span>
    <span>{summary["frontier"]} frontier</span>
    <span>{summary["unlabelled"]} unlabelled</span>
    {warning}
  </div>
</header>
<main>
  <div id="canvas"><svg id="g"><g id="view"></g></svg></div>
  <aside>
    <h2>Selected</h2>
    <div id="detail"></div>
    <h2 style="margin-top:20px">Totals by asset</h2>
    {total_rows or '<div class="row"><span class="k">none</span></div>'}
    <h2 style="margin-top:20px">Legend</h2>
    <div class="legend">{legend}
      <span><i style="border:1px dashed var(--fg);background:none"></i>frontier</span>
    </div>
    <p class="hint">Dashed outlines mark addresses that were seen but never
    expanded. The graph stops there because nobody looked further, not because
    nothing is further. Totals are exact integers in each asset's smallest
    unit &mdash; <code>wei</code>, <code>satoshi</code>, <code>MIST</code>
    &mdash; and are never combined across assets.</p>
  </aside>
</main>
<script>{script}</script>
</body></html>"""
