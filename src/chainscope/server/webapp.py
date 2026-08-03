"""A browsable case: type an address, get the flow, click through it.

Everything this package can do was reachable from a terminal and from an agent,
and the pictures it draws were files you generated and then opened. That is a
fine way to produce a figure for a report and a poor way to *investigate*, which
is a loop — look, notice something, look again somewhere else — and a loop with
a shell command in it is a loop nobody runs twenty times.

So this serves the same store over the same local server the browser extension
already uses, with the layout the commercial tools in this space converged on
because it fits the work:

* **left** — the addresses in view, so you can get back to one you looked at;
* **middle** — the graph, because the shape of a laundering path is the thing
  you actually read;
* **right** — everything known about whatever is selected, and the analyses you
  can run on it without leaving the page.

**It shows the store, and says so.** Nothing here fetches from a chain. The
search box takes an address that is *already* in the case, and when it is not,
the answer says the store has never seen it rather than drawing an empty graph
— those look identical and mean opposite things. Bringing new data in is
`chainscope investigate`, which is a decision about spending somebody's rate
limit and does not belong behind a text field.

**Loopback and same-origin only.** The page is served by the same server that
answers its requests, so a token is not in a URL anybody can copy. The store
holds attributions somebody will act on; the reason `ServerOptions.host`
defaults to `127.0.0.1` applies to the UI exactly as it does to the API.

**Self-contained.** No CDN, no fonts, no map tiles. A forensics tool that phones
a third party on load tells that third party which addresses are being
investigated, which is the one thing it must never do.
"""

from __future__ import annotations

import json

__all__ = ["page"]

_CSS = """
:root {
  --bg: #0e0f13; --panel: #16181f; --line: #262a35; --fg: #e6e8ee;
  --muted: #8b90a0; --accent: #6ea8fe; --warn: #e0a458; --bad: #e06c75;
  --ok: #7fb069;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}
header {
  display: flex; align-items: center; gap: 12px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--panel); flex: none;
}
header h1 { font-size: 14px; margin: 0; font-weight: 700; letter-spacing: .02em; }
header h1 span { color: var(--muted); font-weight: 400; }
form { display: flex; gap: 8px; flex: 1; max-width: 720px; }
input, select, button {
  font: inherit; background: #1d2029; color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px;
}
input { flex: 1; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
input:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
button[disabled] { opacity: .45; cursor: default; }
main { flex: 1; display: grid; grid-template-columns: 260px 1fr 340px; min-height: 0; }
aside {
  background: var(--panel); overflow-y: auto; padding: 12px;
  border-right: 1px solid var(--line); min-height: 0;
}
aside.right { border-right: 0; border-left: 1px solid var(--line); }
h2 {
  font-size: 10px; letter-spacing: .12em; text-transform: uppercase;
  color: var(--muted); margin: 16px 0 8px; font-weight: 600;
}
h2:first-child { margin-top: 0; }
#canvas { position: relative; min-width: 0; }
svg { width: 100%; height: 100%; display: block; cursor: grab; }
svg:active { cursor: grabbing; }
#canvas { position: relative; overflow: hidden; touch-action: none; background:
  radial-gradient(circle at 1px 1px, #1b1e27 1px, transparent 0) 0 0/26px 26px; }
svg { display: block; width: 100%; height: 100%; cursor: grab; }
#canvas:active svg { cursor: grabbing; }
#zoombar {
  position: absolute; right: 14px; bottom: 14px; display: flex; gap: 4px;
  align-items: center; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 4px 6px; font-size: 12px;
}
#zoombar button { padding: 2px 8px; border-radius: 5px; }
#timebar {
  position: absolute; left: 14px; bottom: 14px; display: flex; gap: 8px;
  align-items: center; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 5px 10px; font-size: 11.5px;
}
#time { width: 210px; accent-color: var(--warn); }
#timelab { min-width: 66px; }
#zoom { color: var(--muted); min-width: 44px; text-align: center; }
#addbtn { flex: none; }
dialog {
  background: var(--panel); color: var(--fg); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px; width: min(520px, 90vw);
}
dialog::backdrop { background: rgba(0,0,0,.55); }
dialog h3 { margin: 0 0 6px; font-size: 14px; }
textarea {
  width: 100%; background: #1d2029; color: var(--fg); border: 1px solid var(--line);
  border-radius: 6px; padding: 8px; font: 12px ui-monospace, Menlo, monospace;
  resize: vertical;
}
#addout { max-height: 180px; overflow-y: auto; font-size: 11.5px; margin-top: 6px; }
#addout p { margin: 2px 0; }
.noterow {
  font-size: 11.5px; border-left: 2px solid var(--line); padding: 3px 0 3px 8px;
  margin: 4px 0;
}
.noterow.superseded { opacity: .55; text-decoration: line-through; }
.noterow b { color: var(--accent); font-weight: 600; }
aside.right textarea { margin-top: 4px; }
.card rect { fill: #22252f; stroke: #333846; stroke-width: 1px; cursor: pointer; }
.card:hover rect { stroke: #47506a; }
.card.on rect { stroke: var(--warn); stroke-width: 2px; }
.card.frontier rect { stroke-dasharray: 4 3; }
.card .name { fill: var(--fg); font-size: 11px; font-weight: 600; }
.card .addr {
  fill: #838aa0; font-size: 9.5px;
  font-family: ui-monospace, Menlo, monospace;
}
.card .risk { fill: var(--bad); }
.wm {
  fill: #ffffff; opacity: .05; font-size: 74px; font-weight: 800;
  pointer-events: none; letter-spacing: .06em;
}
#wm { flex: none; width: 110px; }
#spacelab { display: flex; align-items: center; }
#space { width: 88px; accent-color: var(--accent); }
.card text { pointer-events: none; }
.handle rect { fill: #2c3140; stroke: #3d4356; opacity: 0; cursor: pointer; }
.handle text {
  fill: var(--muted); font-size: 12px; text-anchor: middle; opacity: 0;
  pointer-events: none;
}
.card:hover .handle rect, .card.on .handle rect,
.card:hover .handle text, .card.on .handle text { opacity: 1; }
.handle:hover rect { fill: var(--accent); }
.edge { stroke: #5b62d6; fill: none; stroke-width: 1.2px; opacity: .75; }
.edge.lit { stroke: var(--warn); stroke-width: 2px; opacity: 1; }
.elabel { fill: #9aa1b8; font-size: 9.5px; pointer-events: none; }
.elabel.lit { fill: var(--warn); }
.edge.asserted { stroke: var(--ok); stroke-dasharray: 6 4; opacity: 1; }
.elabel.asserted { fill: var(--ok); }
.eidx { fill: #5b62d6; }
.vgroup {
  font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--muted); margin: 10px 0 1px; font-weight: 600;
}
.vgroup.bad { color: var(--bad); }
.vgroup:first-child { margin-top: 0; }
.vwhy { font-size: 10px; color: #5f6577; margin-bottom: 4px; line-height: 1.35; }
label.asset {
  display: grid; grid-template-columns: 16px 1fr auto; gap: 6px;
  align-items: center; padding: 2px 4px; border-radius: 4px; cursor: pointer;
  font-size: 12px;
}
label.asset:hover { background: #1d2029; }
label.asset.off span:not(.muted) { color: var(--muted); text-decoration: line-through; }
label.asset input { margin: 0; accent-color: var(--accent); }
.roster div {
  padding: 5px 6px; border-radius: 5px; cursor: pointer;
  font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.roster div:hover { background: #1d2029; }
.roster div.on { background: #23283a; color: var(--accent); }
.kv { display: grid; grid-template-columns: 88px 1fr; gap: 4px 10px; font-size: 12.5px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; word-break: break-all; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
.muted { color: var(--muted); }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
.pill {
  display: inline-block; padding: 1px 7px; border-radius: 999px;
  font-size: 10.5px; background: #23283a; margin: 0 4px 4px 0;
}
.note {
  font-size: 11.5px; color: var(--muted); border-left: 2px solid var(--line);
  padding-left: 9px; margin: 8px 0;
}
.row { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; }
.row + .sub { color: var(--muted); font-size: 10.5px; margin-top: -3px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.actions button { font-size: 12px; padding: 4px 9px; }
.actions select { font-size: 12px; padding: 4px 6px; flex: 1; }
aside.right input { width: 100%; margin: 4px 0; font-size: 12px; }
#out { white-space: pre-wrap; font-size: 11.5px; margin-top: 8px; }
#status { padding: 6px 16px; border-top: 1px solid var(--line); background: var(--panel);
          font-size: 11.5px; color: var(--muted); flex: none; }
"""

_JS = r"""
const $ = (s) => document.querySelector(s);
const state = {
  graph: null, selected: null, chain: "1",
  // Assets the reader has switched off. Forgeries start off, because a graph
  // whose edges are mostly a forger's own log entries is not the case --- but
  // they are switched off *visibly*, with a count, never dropped.
  hidden: new Set(),
  // Viewport. A fixed picture is fine for a screenshot and useless for a case
  // that does not fit one --- which is every case past about twenty addresses.
  view: { x: 0, y: 0, k: 1 },
  spacing: 1,
  watermark: "",
  // Analyst-drawn edges: a connection somebody asserts, kept apart from the
  // ones the chain shows. See `drawnEdges` below for why they are a different
  // colour and carry a different word.
  drawn: [],
  linking: null,
  // The time window in view. `flow.py` has had this since it was written and
  // this page did not --- so our own older picture answered a question the new
  // one could not: when did this happen, and what does the case look like
  // before that.
  since: null,
};

// View history. Only the *view* --- what is hidden, how it is laid out, what
// was asserted on top. Nothing that reaches the store is in here, because
// everything that reaches the store goes to an append-only log and undoing a
// record is not a thing this tool should offer.
// `trail`, not `history` --- naming it that shadowed `window.history`, so
// `writeUrl`'s `history.replaceState` called this object instead and every
// redraw threw. Found by opening the page; nothing in the type checker or the
// linter can see a global being covered up.
const trail = { past: [], future: [] };

function snapshot() {
  return JSON.stringify({
    hidden: [...state.hidden], spacing: state.spacing,
    watermark: state.watermark, drawn: state.drawn,
  });
}

function remember() {
  const now = snapshot();
  if (trail.past[trail.past.length - 1] === now) return;
  trail.past.push(now);
  if (trail.past.length > 60) trail.past.shift();
  trail.future.length = 0;
  updateHistoryButtons();
}

function restore(shot) {
  const found = JSON.parse(shot);
  state.hidden = new Set(found.hidden);
  state.spacing = found.spacing;
  state.watermark = found.watermark;
  state.drawn = found.drawn;
  $("#wm").value = state.watermark;
  $("#space").value = state.spacing;
  draw(); assets(); count(); applyView(); writeUrl();
}

function undo() {
  if (trail.past.length < 2) return;
  trail.future.push(trail.past.pop());
  restore(trail.past[trail.past.length - 1]);
  updateHistoryButtons();
}

function redo() {
  const next = trail.future.pop();
  if (!next) return;
  trail.past.push(next);
  restore(next);
  updateHistoryButtons();
}

function updateHistoryButtons() {
  $("#undo").disabled = trail.past.length < 2;
  $("#redo").disabled = !trail.future.length;
}

function short(a) { return a.length > 16 ? a.slice(0, 8) + "…" + a.slice(-6) : a; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Amounts arrive as strings because they exceed what a JSON number holds
// exactly, and are formatted on the digits. The same rule as everywhere else in
// this package: never a float, and significant fraction digits rather than a
// fixed cut, so one wei does not render as zero.
function human(raw, decimals) {
  if (decimals === null || decimals === undefined) return raw + " raw";
  const neg = raw.startsWith("-");
  const digits = (neg ? raw.slice(1) : raw).padStart(decimals + 1, "0");
  const whole = digits.slice(0, digits.length - decimals) || "0";
  let frac = decimals ? digits.slice(digits.length - decimals).replace(/0+$/, "") : "";
  if (frac) {
    let keep = 6;
    if (/^0*$/.test(whole)) keep += frac.length - frac.replace(/^0+/, "").length;
    frac = frac.slice(0, keep);
  }
  return (neg ? "-" : "") + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") +
         (frac ? "." + frac : "");
}

async function post(path, body) {
  const reply = await fetch(new URL(path, location.origin), {
    method: "POST",
    headers: { "content-type": "application/json",
               authorization: "Bearer " + TOKEN },
    body: JSON.stringify(body),
  });
  const found = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(found.error || `HTTP ${reply.status}`);
  return found;
}

function say(text, kind) {
  $("#status").innerHTML = kind ? `<span class="${kind}">${esc(text)}</span>` : esc(text);
}

async function api(path, params) {
  const url = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) =>
    v !== undefined && v !== null && url.searchParams.set(k, v));
  const reply = await fetch(url, {
    headers: { accept: "application/json", authorization: "Bearer " + TOKEN },
  });
  const body = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(body.error || `HTTP ${reply.status}`);
  return body;
}

// ------------------------------------------------------------------ drawing
//
// Cards, not dots. A dot with a truncated label beside it makes you click to
// learn anything; a card carries the entity name AND the full address, so the
// shape of the case is readable without touching it. Studied from the
// commercial tools, which all converged here.
//
// The edge label is the other half and matters more: `[time] amount asset`
// sits on the line, so a flow reads as a sentence rather than as a thing to
// interrogate.

const CARD_W = 218, CARD_H = 52, GAP_Y = 22;

function readUrl() {
  // The view restored from the address bar. That is what a "share link" can
  // honestly be for a local tool: the data lives in somebody's store and is
  // not ours to send, but *what was being looked at* is a real thing to hand
  // over --- the seed, the chain, and which assets were hidden.
  const q = new URLSearchParams(location.search);
  const address = q.get("a") || "";
  if (q.get("c")) state.chain = q.get("c");
  if (q.get("h")) state.pendingHidden = new Set(q.get("h").split(","));
  if (q.get("w")) state.watermark = q.get("w");
  return address;
}

function writeUrl() {
  const q = new URLSearchParams();
  if (state.graph) q.set("a", state.graph.seed);
  q.set("c", state.chain);
  if (state.hidden.size) q.set("h", [...state.hidden].join(","));
  if (state.watermark) q.set("w", state.watermark);
  history.replaceState(null, "", "?" + q.toString());
}

function layout(graph) {
  // By hop depth, left to right. A force layout arranges by connectivity, and
  // then a five-hop laundering chain and a five-way split look identical ---
  // the distinction a reader most needs is the one it destroys.
  const columns = new Map();
  graph.nodes.forEach((n) => {
    if (!columns.has(n.depth)) columns.set(n.depth, []);
    columns.get(n.depth).push(n);
  });
  const depths = [...columns.keys()].sort((a, b) => a - b);
  let height = 0;
  depths.forEach((d, i) => {
    const column = columns.get(d);
    column.forEach((n, j) => {
      n.x = 40 + i * (CARD_W + 210 * state.spacing);
      n.y = 30 + j * (CARD_H + GAP_Y * state.spacing);
    });
    height = Math.max(height,
      30 + column.length * (CARD_H + GAP_Y * state.spacing));
  });
  return {
    width: 80 + depths.length * (CARD_W + 210 * state.spacing),
    height: Math.max(height, 200),
  };
}

function edgePath(a, b) {
  // Leaves the source's right edge, turns once, arrives at the target's left.
  // Bundling out of one point is what makes a fan of twelve payments legible
  // instead of twelve lines crossing the same space.
  const x1 = a.x + CARD_W, y1 = a.y + CARD_H / 2;
  const x2 = b.x, y2 = b.y + CARD_H / 2;
  const mid = x1 + Math.max(40, (x2 - x1) * 0.45);
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

function litEdges(graph, selected) {
  // Every edge on a path from a seed to the selection, not the shortest one:
  // a split that rejoins is the structure worth seeing.
  const lit = new Set();
  if (!selected) return lit;
  const back = new Map();
  graph.edges.forEach((e) => {
    if (!back.has(e.target)) back.set(e.target, []);
    back.get(e.target).push(e);
  });
  const queue = [selected], seen = new Set([selected]);
  while (queue.length) {
    const at = queue.pop();
    (back.get(at) || []).forEach((e) => {
      lit.add(e.source + ">" + e.target);
      if (!seen.has(e.source)) { seen.add(e.source); queue.push(e.source); }
    });
  }
  return lit;
}

function draw() {
  const graph = state.graph, svg = $("#g");
  if (!graph) { svg.innerHTML = ""; return; }
  const size = layout(graph);
  svg.setAttribute("viewBox", `0 0 ${size.width} ${size.height}`);
  svg.setAttribute("width", size.width);
  svg.setAttribute("height", size.height);

  const byId = new Map(graph.nodes.map((n) => [n.address, n]));
  const lit = litEdges(graph, state.selected);
  const parts = [
    `<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4"
       markerWidth="6" markerHeight="6" orient="auto">
       <path d="M0,0 L8,4 L0,8z" fill="#5b62d6"/></marker>
     <marker id="arrowlit" viewBox="0 0 8 8" refX="7" refY="4"
       markerWidth="6" markerHeight="6" orient="auto">
       <path d="M0,0 L8,4 L0,8z" fill="#e0a458"/></marker>
     <marker id="arrowassert" viewBox="0 0 8 8" refX="7" refY="4"
       markerWidth="6" markerHeight="6" orient="auto">
       <path d="M0,0 L8,4 L0,8z" fill="#7fb069"/></marker></defs>`,
  ];

  graph.edges.forEach((e, i) => {
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) return;
    if (state.hidden.has(e.asset || "")) return;
    // An edge is an aggregate over a span, so it is in view when the span
    // *reaches* the cursor --- testing `first` alone would hide a flow that
    // began earlier and was still running.
    if (state.since !== null && (e.last_seen ?? 0) > state.since) return;
    const on = lit.has(e.source + ">" + e.target);
    parts.push(
      `<path class="edge${on ? " lit" : ""}" d="${edgePath(a, b)}"` +
      ` marker-end="url(#${on ? "arrowlit" : "arrow"})"/>`);
    // The label rides above the line, close to the source, the way a
    // statement of what happened should sit next to what it happened on.
    const when = e.first_seen
      ? new Date(e.first_seen * 1000).toISOString().slice(0, 16).replace("T", " ")
      : "undated";
    const amount = `${human(e.total_raw, e.decimals)} ${esc(e.symbol || "native")}`;
    // Positioned by the TARGET's row, not the source's. Twelve payments
    // leaving one address all depart from the same point, so anchoring the
    // label there stacks twelve lines of text on top of each other --- which
    // is what the first version did, and it made the most useful thing on the
    // screen the least readable.
    parts.push(
      `<text class="elabel${on ? " lit" : ""}" x="${b.x - 12}"` +
      ` y="${b.y + CARD_H / 2 - 6}" text-anchor="end">` +
      `<tspan class="eidx">[${i + 1}]</tspan> [${when}] ${amount}</text>`);
  });

  // Asserted edges, drawn distinctly and labelled with who said so. A line
  // somebody drew and a line the chain shows must never look alike: the whole
  // argument of this tool is that a claim carries its provenance, and an edge
  // is a claim.
  state.drawn.forEach((d) => {
    const a = byId.get(d.source), b = byId.get(d.target);
    if (!a || !b) return;
    parts.push(`<path class="edge asserted" d="${edgePath(a, b)}"` +
      ` marker-end="url(#arrowassert)"/>`);
    parts.push(`<text class="elabel asserted" x="${b.x - 12}"` +
      ` y="${b.y + CARD_H / 2 - 6}" text-anchor="end">` +
      `asserted: ${esc(d.why || "no reason given")}</text>`);
  });

  graph.nodes.forEach((n) => {
    const on = n.address === state.selected;
    const risky = ["sanctioned", "scam", "illicit", "mixer"].includes(n.category);
    parts.push(
      `<g class="card${on ? " on" : ""}${n.frontier ? " frontier" : ""}"` +
      ` data-a="${esc(n.address)}" transform="translate(${n.x},${n.y})">` +
      `<rect width="${CARD_W}" height="${CARD_H}" rx="7"/>` +
      `<circle class="chip" cx="17" cy="${CARD_H / 2}" r="9" fill="` +
      `${PALETTE[n.category] || (n.seed ? "#6ea8fe" : "#4a5570")}"/>` +
      (risky ? `<circle class="risk" cx="${CARD_W - 8}" cy="8" r="4"/>` : "") +
      `<text class="name" x="34" y="20">` +
      `${esc((n.label || (n.seed ? "seed" : "unlabelled")).slice(0, 26))}</text>` +
      `<text class="addr" x="34" y="34">` +
      `${esc(n.address.slice(0, 26))}</text>` +
      `<text class="addr" x="34" y="45">${esc(n.address.slice(26))}</text>` +
      // Expand handles, left and right. The best idea in any of these tools:
      // grow the picture in the direction the money went, from the thing you
      // are already looking at, rather than from a form somewhere else.
      `<g class="handle" data-grow="in" data-a="${esc(n.address)}">` +
      `<rect x="-9" y="${CARD_H / 2 - 8}" width="16" height="16" rx="4"/>` +
      `<text x="-1" y="${CARD_H / 2 + 5}">+</text></g>` +
      `<g class="handle" data-grow="out" data-a="${esc(n.address)}">` +
      `<rect x="${CARD_W - 7}" y="${CARD_H / 2 - 8}" width="16" height="16" rx="4"/>` +
      `<text x="${CARD_W + 1}" y="${CARD_H / 2 + 5}">+</text></g>` +
      `</g>`);
  });

  if (state.watermark) {
    // Inside the SVG, not a CSS overlay --- so it is present in the exported
    // file too. A watermark that vanishes on export is decoration.
    parts.unshift(`<text class="wm" x="${size.width / 2}" y="${size.height / 2}"` +
      ` text-anchor="middle">${esc(state.watermark)}</text>`);
  }
  svg.innerHTML = `<g id="vp" transform="translate(${state.view.x},${state.view.y})` +
    ` scale(${state.view.k})">${parts.join("")}</g>`;
  svg.querySelectorAll(".card").forEach((g) =>
    g.addEventListener("click", (ev) => {
      if (ev.target.closest(".handle")) return;
      if (state.linking) { finishLink(g.dataset.a); return; }
      select(g.dataset.a);
    }));
  svg.querySelectorAll(".handle").forEach((h) =>
    h.addEventListener("click", (ev) => {
      ev.stopPropagation();
      load(h.dataset.a);
    }));
}

const PALETTE = {
  sanctioned: "#e06c75", mixer: "#c678dd", cex: "#61afef", dex: "#98c379",
  bridge: "#c678dd", illicit: "#e0a458", scam: "#e06c75", service: "#56b6c2",
};

// ------------------------------------------------------------------ panels

const VERDICT_NOTE = {
  genuine: "matches the canonical contract for its symbol",
  forged: "claims a symbol that belongs to another contract on this chain",
  lookalike: "renders identically to a real symbol under UTS #39",
  "unknown-script": "built from characters outside ASCII",
  unlisted: "no canonical entry --- says nothing either way",
};

function applyView() {
  const vp = document.getElementById("vp");
  if (vp) {
    vp.setAttribute("transform",
      `translate(${state.view.x},${state.view.y}) scale(${state.view.k})`);
  }
  $("#zoom").textContent = Math.round(state.view.k * 100) + "%";
}

function zoomBy(factor, cx, cy) {
  // Around the cursor, not the origin. Zooming to a corner makes the reader
  // chase the thing they were looking at, which is most of what makes a graph
  // tool feel broken.
  const k = Math.min(4, Math.max(0.15, state.view.k * factor));
  const box = $("#canvas").getBoundingClientRect();
  const px = (cx ?? box.width / 2) - box.left;
  const py = (cy ?? box.height / 2) - box.top;
  state.view.x = px - ((px - state.view.x) * k) / state.view.k;
  state.view.y = py - ((py - state.view.y) * k) / state.view.k;
  state.view.k = k;
  applyView();
}

function fit() {
  const graph = state.graph;
  if (!graph || !graph.nodes.length) return;
  const size = layout(graph);
  const box = $("#canvas").getBoundingClientRect();
  const k = Math.min(1, Math.min(box.width / (size.width + 60),
                                 box.height / (size.height + 60)));
  state.view = {
    k,
    x: (box.width - size.width * k) / 2,
    y: (box.height - size.height * k) / 2,
  };
  applyView();
}

function startLink() {
  if (!state.selected) { say("select the address the link starts from", "bad"); return; }
  state.linking = state.selected;
  say(`linking from ${short(state.linking)} — click the address it reaches`);
}

async function finishLink(target) {
  const source = state.linking;
  state.linking = null;
  if (!source || source === target) return;
  const why = prompt(
    "Why do these belong together?\n\n" +
    "This is recorded as a NOTE against both addresses, with your name and the " +
    "time, because it is a claim you are making rather than something the chain " +
    "shows. The line is drawn in a different colour for the same reason.");
  if (!why || !why.trim()) { say("nothing drawn — an assertion needs a reason"); return; }
  try {
    // Written to the case log, not only to the canvas. A line that lives in a
    // picture is lost when the picture is redrawn, and carries no author.
    await post("/note", {
      body: `asserted link to ${target}: ${why.trim()}`,
      kind: "observation", subject: source, chain: state.chain,
    });
    state.drawn.push({ source, target, why: why.trim() });
    remember();
    draw();
    say(`asserted ${short(source)} → ${short(target)}, filed as a note`);
  } catch (err) {
    say(err.message, "bad");
  }
}

function exportSvg() {
  // The SVG itself, with its styles inlined. Self-contained by the same rule
  // as everything else here: a picture that fetches a stylesheet to render is
  // a picture that stops rendering, and tells somebody it was opened.
  const svg = $("#g").cloneNode(true);
  svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const size = layout(state.graph);
  svg.setAttribute("width", size.width);
  svg.setAttribute("height", size.height);
  svg.querySelector("#vp").setAttribute("transform", "translate(20,20)");
  const style = document.createElement("style");
  style.textContent = document.querySelector("style").textContent;
  svg.insertBefore(style, svg.firstChild);
  const blob = new Blob(
    ['<?xml version="1.0" encoding="UTF-8"?>\n', svg.outerHTML],
    { type: "image/svg+xml" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${(state.graph?.seed || "case").slice(0, 12)}.svg`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function wireTools() {
  $("#space").addEventListener("input", (ev) => {
    state.spacing = Number(ev.target.value);
    draw(); applyView(); remember();
  });
  $("#wm").addEventListener("input", (ev) => {
    state.watermark = ev.target.value.trim();
    draw(); writeUrl(); remember();
  });
  $("#export").addEventListener("click", exportSvg);
  $("#link").addEventListener("click", startLink);
  $("#undo").addEventListener("click", undo);
  $("#redo").addEventListener("click", redo);
  document.addEventListener("keydown", (ev) => {
    if (!(ev.metaKey || ev.ctrlKey)) return;
    if (ev.key === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); }
    if (ev.key === "z" && ev.shiftKey) { ev.preventDefault(); redo(); }
  });
  $("#share").addEventListener("click", async () => {
    writeUrl();
    try {
      await navigator.clipboard.writeText(location.href);
      say("link copied — it restores this view, not the data behind it");
    } catch {
      say("copy the address bar: it now holds this view");
    }
  });
}

function wireAdd() {
  const dialog = $("#adddlg");
  $("#addbtn").addEventListener("click", () => dialog.showModal());
  $("#addclose").addEventListener("click", () => dialog.close());
  $("#addgo").addEventListener("click", async () => {
    const wanted = $("#addtext").value.split(/[\s,]+/)
      .map((a) => a.trim()).filter(Boolean);
    if (!wanted.length) return;
    const out = $("#addout");
    // One at a time, with the count visible. A batch that reports only at the
    // end looks identical to a hung one, and these are network calls.
    for (let i = 0; i < wanted.length; i++) {
      out.innerHTML = `<p class="muted">${i + 1} of ${wanted.length}: ` +
        `${esc(short(wanted[i]))}…</p>` + out.innerHTML;
      try {
        const found = await api("/graph", { address: wanted[i], chain: state.chain });
        out.innerHTML = `<p>${esc(short(wanted[i]))} — ${found.fetched} fetched` +
          `${found.fetch_complete ? "" : " (prefix — page budget spent)"}</p>` +
          out.innerHTML;
      } catch (err) {
        out.innerHTML = `<p class="bad">${esc(short(wanted[i]))} — ` +
          `${esc(err.message)}</p>` + out.innerHTML;
      }
    }
    // Reload the seed's graph: the new addresses are in the store now and the
    // walk may reach them.
    if (state.graph) await load(state.graph.seed);
  });
}

function wireViewport() {
  const canvas = $("#canvas");
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomBy(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX, ev.clientY);
  }, { passive: false });

  let dragging = null;
  canvas.addEventListener("pointerdown", (ev) => {
    // Only on empty canvas --- dragging from a card would fight the click that
    // selects it.
    if (ev.target.closest(".card")) return;
    dragging = { x: ev.clientX - state.view.x, y: ev.clientY - state.view.y };
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!dragging) return;
    state.view.x = ev.clientX - dragging.x;
    state.view.y = ev.clientY - dragging.y;
    applyView();
  });
  canvas.addEventListener("pointerup", () => { dragging = null; });

  $("#zin").addEventListener("click", () => zoomBy(1.2));
  $("#zout").addEventListener("click", () => zoomBy(1 / 1.2));
  $("#zfit").addEventListener("click", fit);
}

function assets() {
  const rows = (state.graph && state.graph.assets) || [];
  if (!rows.length) { $("#assets").innerHTML = ""; return; }
  // Grouped by verdict, not listed flat. A filter offering forty tickers in one
  // column makes the reader do the classification, and the tickers were chosen
  // by the forger to make that go wrong --- three read `ETH` on a real case.
  const groups = ["forged", "lookalike", "unknown-script", "genuine", "unlisted"];
  const out = [];
  groups.forEach((verdict) => {
    const mine = rows.filter((r) => r.verdict === verdict);
    if (!mine.length) return;
    const suspect = ["forged", "lookalike", "unknown-script"].includes(verdict);
    out.push(`<div class="vgroup${suspect ? " bad" : ""}">${esc(verdict)}` +
      ` <span class="muted">${mine.length}</span></div>`);
    out.push(`<div class="vwhy">${esc(VERDICT_NOTE[verdict] || "")}</div>`);
    mine.forEach((r) => {
      const off = state.hidden.has(r.asset);
      out.push(`<label class="asset${off ? " off" : ""}" title="${esc(r.why)}">` +
        `<input type="checkbox" data-asset="${esc(r.asset)}"${off ? "" : " checked"}>` +
        `<span>${esc(r.symbol || "native")}</span>` +
        `<span class="muted">${r.transfers}</span></label>`);
    });
  });
  $("#assets").innerHTML = out.join("");
  $("#assets").querySelectorAll("input").forEach((box) =>
    box.addEventListener("change", () => {
      if (box.checked) state.hidden.delete(box.dataset.asset);
      else state.hidden.add(box.dataset.asset);
      draw(); assets(); count(); writeUrl();
    }));
}

function timeRange() {
  const times = (state.graph?.edges || [])
    // `first_seen`, which is what the payload calls it. Reading `e.first` gave
    // undefined for every edge, so the range was empty and the scrubber hid
    // itself --- a control that silently decides it has nothing to do.
    .map((e) => e.first_seen).filter((t) => t);
  return times.length ? [Math.min(...times), Math.max(...times)] : null;
}

function wireTime() {
  const scrub = $("#time"), label = $("#timelab");
  const span = timeRange();
  if (!span || span[0] === span[1]) {
    // Nothing to scrub. Hidden rather than shown inert, because a control that
    // does nothing reads as a control that is broken.
    $("#timebar").style.display = "none";
    return;
  }
  $("#timebar").style.display = "flex";
  scrub.min = String(span[0]);
  scrub.max = String(span[1]);
  scrub.value = String(span[1]);
  state.since = null;
  label.textContent = "all";
  scrub.oninput = () => {
    const at = Number(scrub.value);
    state.since = at >= span[1] ? null : at;
    label.textContent = state.since === null
      ? "all"
      : new Date(at * 1000).toISOString().slice(0, 10);
    draw(); count();
  };
}

function count() {
  const graph = state.graph;
  if (!graph) return;
  const shown = graph.edges.filter((e) => !state.hidden.has(e.asset || "")).length;
  const bits = [`${graph.nodes.length} addresses`, `${shown} of ${graph.edges.length} flows`];
  if (state.since !== null) {
    // Said with a count, like the asset filter. A picture narrowed in time
    // that does not announce it is the same defect as a truncated one.
    const hidden = graph.edges.filter((e) => (e.last_seen ?? 0) > state.since).length;
    bits.push(`${hidden} flow(s) after the time you set are hidden`);
  }
  if (state.hidden.size) {
    // Said, with a number. A filtered picture that does not announce itself is
    // the same defect as a truncated one.
    bits.push(`${graph.edges.length - shown} hidden by an asset filter you set`);
  }
  if (graph.fetched) {
    bits.push(`${graph.fetched} transfers fetched from a provider` +
      (graph.fetch_complete ? "" : " — PAGE BUDGET SPENT, this is a prefix"));
  }
  say(bits.join(" · ") + (graph.truncated
    ? " · TRUNCATED — a limit stopped the walk; this is not the whole case"
    : ""), graph.truncated || !graph.fetch_complete ? "warn" : null);
}

function roster() {
  const graph = state.graph;
  if (!graph) return;
  $("#roster").innerHTML = graph.nodes
    .map((n) => `<div data-a="${esc(n.address)}"` +
      ` class="${n.address === state.selected ? "on" : ""}">` +
      `${esc(n.label || short(n.address))}</div>`).join("");
  $("#roster").querySelectorAll("div").forEach((d) =>
    d.addEventListener("click", () => select(d.dataset.a)));
}

async function select(address) {
  state.selected = address;
  draw(); roster();
  const panel = $("#detail");
  panel.innerHTML = '<p class="muted">reading the store…</p>';
  try {
    const found = await api("/resolve", { address, chain: state.chain });
    // `found.key`, not the address as typed. The server applies the per-chain
    // normalisation and hands back the identity the node list uses; comparing
    // the raw string matched nothing whenever a user typed a checksummed
    // address, and the panel then said hop "—" for an address plainly on
    // screen. A second copy of the rule in JavaScript is what would drift.
    const key = found.key || address;
    const node = (state.graph?.nodes || []).find(
      (n) => n.address === key || n.address === address) || {};
    const rows = [
      ["address", `<span class="mono">${esc(address)}</span>`],
      ["hop", node.depth ?? "—"],
      // Three states. "Unlabelled" is a finding; "we could not ask" is not,
      // and rendering both as the same grey word is how a missing data file
      // becomes a clean screening result.
      ["label", found.claims.length
        ? esc(found.claims[0].label)
        : (found.reliable === false
            ? '<span class="bad">unknown — a source failed, see below</span>'
            : '<span class="muted">none — unlabelled, not unimportant</span>')],
    ];
    if (found.claims.length) {
      rows.push(["category", esc(found.claims[0].category)]);
      rows.push(["confidence", esc(found.claims[0].confidence)]);
      rows.push(["source", esc(found.claims[0].source)]);
    }
    const pairs = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    let html = `<dl class="kv">${pairs}</dl>`;
    if (found.reliable === false) {
      html += '<p class="note bad">Incomplete: ' +
        (found.unreachable_sources || []).map(esc).join("; ") +
        '. An empty result here is not a clean one.</p>';
    }
    if (node.frontier) {
      html += '<p class="note">Frontier. Its counterparties were never fetched — ' +
              'the picture stops here because nobody looked, not because there is nothing.</p>';
    }
    // Labelling is the point of the tool, and it lived only in the CLI. The
    // form states what the store will record rather than only what was typed:
    // a claim carries a source and a confidence whether or not the person
    // filling it in thinks about them, so it should say which.
    html += '<h2>label it</h2>' +
      `<input id="lab" placeholder="what this address is" value="${
        esc(found.claims.length ? found.claims[0].label : "")}">` +
      '<div class="actions">' +
      '<select id="cat">' +
      ["service", "cex", "dex", "bridge", "mixer", "scam", "illicit",
       "sanctioned", "contract", "token", "suspect", "unknown"]
        .map((c) => `<option>${c}</option>`).join("") +
      '</select>' +
      '<select id="conf">' +
      ["medium", "high", "low", "certain", "speculative"]
        .map((c) => `<option>${c}</option>`).join("") +
      '</select>' +
      '<button id="save">record</button></div>' +
      '<input id="why" placeholder="why — required below medium">' +
      '<p class="note">Recorded with this browser named as the source. A claim ' +
      'that picks its own provenance is worse than one carrying none, so the ' +
      'store will always say a browser wrote it.</p>' +
      '<h2>notes</h2><div id="notes"></div>' +
      '<textarea id="notebody" rows="2" placeholder="what you noticed"></textarea>' +
      '<div class="actions"><select id="notekind">' +
      ["observation", "question", "decision"]
        .map((k) => `<option>${k}</option>`).join("") +
      '</select><button id="notesave">file</button></div>' +
      '<p class="note">Filed in the case log --- append-only, authored, timed. ' +
      'Not a sticky on the drawing: a note that lives in a picture is lost when ' +
      'the picture is redrawn, and cannot answer "what is still open".</p>' +
      '<h2>run on this address</h2><div class="actions">' +
      ["impersonation", "poisoning", "contributors"].map((a) =>
        `<button data-run="${a}">${a}</button>`).join("") + "</div><div id='out'></div>";
    panel.innerHTML = html;
    panel.querySelectorAll("[data-run]").forEach((b) =>
      b.addEventListener("click", () => runAnalysis(b.dataset.run, address)));
    loadNotes(address);
    const filed = $("#notesave");
    if (filed) {
      filed.addEventListener("click", async () => {
        const text = $("#notebody").value.trim();
        if (!text) { say("a note needs a body", "bad"); return; }
        filed.disabled = true;
        try {
          await post("/note", {
            body: text, kind: $("#notekind").value,
            subject: address, chain: state.chain,
          });
          $("#notebody").value = "";
          await loadNotes(address);
          say("note filed");
        } catch (err) {
          say(err.message, "bad");
        } finally {
          filed.disabled = false;
        }
      });
    }
    const save = $("#save");
    if (save) {
      save.addEventListener("click", async () => {
        const label = $("#lab").value.trim();
        if (!label) { say("a label needs text", "bad"); return; }
        save.disabled = true;
        try {
          await post("/tag", {
            address,
            label,
            category: $("#cat").value,
            confidence: $("#conf").value,
            rationale: $("#why").value.trim(),
            chain: state.chain,
          });
          say(`recorded "${label}" for ${short(address)}`);
          // Redraw: the label belongs on the card, and seeing it appear there
          // is the confirmation that it was written.
          await load(state.graph.seed);
        } catch (err) {
          say(err.message, "bad");
        } finally {
          save.disabled = false;
        }
      });
    }
  } catch (err) {
    panel.innerHTML = `<p class="bad">${esc(err.message)}</p>`;
  }
}

async function loadNotes(address) {
  const box = $("#notes");
  if (!box) return;
  try {
    const found = await api("/notes", { subject: address });
    box.innerHTML = found.notes.length
      ? found.notes.map((n) => {
          // Superseded notes are shown struck through, not removed. "I thought
          // X, then found Y" is the record; a log showing only the final
          // position reads like one that was right the first time.
          const gone = n.superseded ? " superseded" : "";
          return `<div class="noterow${gone}"><b>${esc(n.kind)}</b> ` +
            `${esc(n.body)}<br><span class="muted">${esc(n.analyst)} · ` +
            `${esc(n.at.slice(0, 16).replace("T", " "))}</span></div>`;
        }).join("")
      : '<p class="muted">none filed against this address</p>';
  } catch (err) {
    box.innerHTML = `<p class="bad">${esc(err.message)}</p>`;
  }
}

async function runAnalysis(name, address) {
  const out = $("#out");
  out.innerHTML = '<span class="muted">running…</span>';
  try {
    const found = await api("/analyze", { name, address, chain: state.chain,
                                          subject: state.graph?.seed });
    let html = "";
    (found.warnings || []).forEach((w) => (html += `<p class="note warn">${esc(w)}</p>`));
    (found.findings || []).forEach((f) => {
      html += `<p><b>${esc(f.title)}</b><br><span class="muted">${esc(f.detail)}</span></p>`;
    });
    (found.hypotheses || []).forEach((h) => {
      html += `<p><b>${esc(h.claim)}</b> <span class="pill">${esc(h.confidence)}</span><br>` +
        h.factors.map((x) => `<span class="muted">${x.contribution >= 0 ? "+" : ""}` +
          `${x.contribution} ${esc(x.name)} — ${esc(x.note)}</span>`).join("<br>") + "</p>";
    });
    out.innerHTML = html || '<p class="muted">nothing found. That is a statement about ' +
      'what is in the store, not about the address.</p>';
  } catch (err) {
    out.innerHTML = `<p class="bad">${esc(err.message)}</p>`;
  }
}

// ------------------------------------------------------------------ loading

async function load(address) {
  say("reading the store…");
  try {
    const graph = await api("/graph", { address, chain: state.chain });
    state.graph = graph;
    state.selected = graph.seed;
    // Forgeries start hidden. A graph whose edges are mostly a forger's own
    // log entries is not the case --- and the status line says how many, so
    // this is a stated default rather than a silent one.
    state.hidden = new Set(
      (graph.assets || [])
        .filter((a) => ["forged", "lookalike", "unknown-script"].includes(a.verdict))
        .map((a) => a.asset));
    if (state.pendingHidden) {
      state.hidden = state.pendingHidden;
      state.pendingHidden = null;
    }
    draw(); roster(); assets(); wireTime(); select(graph.seed); count(); fit();
    writeUrl();
    trail.past.length = 0; trail.future.length = 0; remember();
    const bits = [`${graph.nodes.length} addresses`, `${graph.edges.length} flows`];
    // Said, not hidden. The page reads the store; when it had to go to a
    // provider to fill it, that is a fact about where the picture came from.
    if (graph.fetched) {
      bits.push(`${graph.fetched} transfers fetched from a provider` +
        (graph.fetch_complete ? "" : " — PAGE BUDGET SPENT, this is a prefix"));
    }
    if (graph.frontier) bits.push(`${graph.frontier} on the frontier`);
    say(bits.join(" · ") + (graph.truncated
      ? " · TRUNCATED — a limit stopped the walk; this is not the whole case"
      : ""), graph.truncated ? "warn" : null);
  } catch (err) {
    state.graph = null; draw();
    $("#roster").innerHTML = ""; $("#detail").innerHTML = "";
    say(err.message, "bad");
  }
}

$("#find").addEventListener("submit", (e) => {
  e.preventDefault();
  state.chain = $("#chain").value;
  const address = $("#address").value.trim();
  if (address) load(address);
});
window.addEventListener("resize", () => { draw(); applyView(); });
wireViewport();
wireAdd();
wireTools();

const fromUrl = readUrl();
if (fromUrl) {
  $("#address").value = fromUrl;
  $("#chain").value = state.chain;
  if (state.watermark) $("#wm").value = state.watermark;
  load(fromUrl);
}

api("/health").then((h) => {
  say(`${h.transfers ?? 0} transfers in ${h.store || "the store"} · ` +
      `this page reads what is already there and never fetches from a chain`);
}).catch(() => say("the server is not answering", "bad"));
"""

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>chainscope</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- No external anything. A forensics tool that fetches from a third party on
     load tells that party which addresses are under investigation. -->
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
               connect-src 'self'; img-src data:">
<style>__CSS__</style></head>
<body>
<header>
  <h1>chainscope <span>&mdash; the case in this store</span></h1>
  <form id="find">
    <input id="address" placeholder="address already in the case" spellcheck="false"
           autocomplete="off">
    <select id="chain">
      <option value="1">Ethereum</option>
      <option value="56">BSC</option>
      <option value="137">Polygon</option>
      <option value="8453">Base</option>
      <option value="42161">Arbitrum</option>
    </select>
    <button type="submit">open</button>
  </form>
  <button id="addbtn" title="add addresses to this case">+ add</button>
  <button id="share" title="copy a link that restores this view">share</button>
  <button id="export" title="download the graph as SVG">export</button>
  <button id="link" title="assert a link between two addresses">link</button>
  <button id="undo" title="undo (cmd-Z)" disabled>&#8630;</button>
  <button id="redo" title="redo (cmd-shift-Z)" disabled>&#8631;</button>
  <input id="wm" placeholder="watermark" size="10">
  <label id="spacelab" title="spacing">
    <input id="space" type="range" min="0.5" max="2.5" step="0.1" value="1">
  </label>
</header>
<main>
  <aside>
    <h2>assets</h2><div id="assets"></div>
    <h2>addresses in view</h2><div class="roster" id="roster"></div>
  </aside>
  <div id="canvas"><svg id="g"></svg>
    <div id="timebar" style="display:none">
      <span class="muted">up to</span>
      <input id="time" type="range"><span id="timelab" class="muted">all</span>
    </div>
    <div id="zoombar">
      <button id="zfit" title="fit to the case">⌖</button>
      <button id="zout">&minus;</button><span id="zoom">100%</span>
      <button id="zin">+</button>
    </div>
  </div>
  <aside class="right"><h2>selected</h2><div id="detail">
    <p class="muted">Enter an address that is already in the case.</p>
    <p class="note">This page reads the store. It never fetches from a chain &mdash;
    bringing new data in is <span class="mono">chainscope investigate</span>, which
    spends somebody's rate limit and should not sit behind a text field.</p>
  </div></aside>
</main>
<dialog id="adddlg">
  <h3>Add to this case</h3>
  <p class="note">One per line. Each is fetched and merged into the store, so
  the graph you are looking at grows rather than being replaced. Anything
  already present is skipped, not fetched again.</p>
  <textarea id="addtext" rows="7" spellcheck="false"
    placeholder="0x… one address per line"></textarea>
  <div class="actions">
    <button id="addgo">add</button>
    <button id="addclose">cancel</button>
  </div>
  <div id="addout"></div>
</dialog>
<div id="status"></div>
<script>const TOKEN = "__TOKEN__";__JS__</script>
</body></html>
"""


def page(token: str) -> str:
    """The page, with the API token baked in.

    Rather than in a URL. Whoever can fetch this HTML can already reach the
    server --- it is loopback and same-origin --- so the token is no weaker
    here, and a URL is the one place a credential gets copied into a bug
    report, a screenshot, or somebody's shell history.

    JSON-encoded, so a token containing a quote cannot end the string it sits
    in and start being code.
    """
    return (
        _TEMPLATE.replace("__CSS__", _CSS)
        # Before `__JS__`, because the script itself contains the placeholder's
        # name in a comment. Substituting in the other order would rewrite that
        # comment and leave the real one alone --- the same ordering mistake
        # that once left `__PALETTE__` unsubstituted in the flow renderer.
        .replace('"__TOKEN__"', json.dumps(token))
        .replace("__JS__", _JS)
    )
