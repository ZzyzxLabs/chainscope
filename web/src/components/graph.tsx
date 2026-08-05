"use client";

/**
 * The drawing.
 *
 * SVG rather than canvas: the nodes are text, and text in a canvas cannot be
 * selected, searched with the browser's own find, or read by a screen reader.
 * An investigator copying an address out of the picture is a thing that happens
 * constantly, and canvas turns it into retyping from a screenshot.
 *
 * Two states are drawn differently on purpose, and both are cases where a
 * complete-looking picture would be wrong:
 *
 *   * a **frontier** node has a dashed border — its counterparties were never
 *     fetched, so the picture stops there because nobody looked;
 *   * a **truncated** graph says so in the status bar rather than simply being
 *     smaller.
 *
 * **Not every flow can carry its label, and the drawing says how many do not.**
 * Forty-nine labels in one column overlap into a block nobody can read, which
 * loses all forty-nine rather than the twelve that would not fit. So the label
 * budget is finite, `labelMode` says which rule filled it, and the count of
 * unlabelled flows is stated. Every flow keeps its full text on hover
 * regardless, because a hidden label is a display decision and must not become
 * a missing fact.
 *
 * The order is by **transfer count**, which is a count of events and therefore
 * comparable between assets. Ranking by amount was the obvious alternative and
 * is wrong here: 500 USDC and 500 SHIB are not orderable, and a picture that
 * silently implies they are would be this package asserting something it
 * cannot support, in a sort.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { GraphEdge, GraphNode } from "@/lib/api";
import {
  CARD_H,
  CARD_W,
  type EdgeLabel,
  edgeMid,
  edgePath,
  human,
  layout,
  litEdges,
  placeLabels,
  short,
  when,
} from "@/lib/layout";

/**
 * How many labels fit before a column of them stops being readable.
 *
 * Measured on the drawing rather than picked: at this line height, a column of
 * more than about a dozen runs into the next node down and the de-collision
 * pass starts pushing labels off their own edges.
 */
const LABEL_BUDGET = 14;

type LabelMode = "auto" | "all" | "none";

type Props = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected: string | null;
  onSelect: (address: string) => void;
  /** Assets the reader has switched off. */
  hidden: Set<string>;
  /** Hide flows whose span had not reached this instant. Null shows all. */
  since: number | null;
  spacing: number;
  watermark: string;
  /** Lets the page serialise the drawing for export. */
  svgRef?: React.RefObject<SVGSVGElement | null>;
};

export function Graph({
  nodes,
  edges,
  selected,
  onSelect,
  hidden,
  since,
  spacing,
  watermark,
  svgRef,
}: Props) {
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 });
  const [labelMode, setLabelMode] = useState<LabelMode>("auto");
  const [key, setKey] = useState(false);
  const dragging = useRef<{ x: number; y: number } | null>(null);
  const frame = useRef<HTMLDivElement | null>(null);

  const { placed, width, height } = useMemo(() => layout(nodes, spacing), [nodes, spacing]);
  const byId = useMemo(() => new Map(placed.map((n) => [n.address, n])), [placed]);
  const lit = useMemo(() => litEdges(edges, selected), [edges, selected]);

  const visible = useMemo(
    () =>
      edges.filter((edge) => {
        if (hidden.has(edge.asset || "")) return false;
        // An edge aggregates a span, so it is in view once that span *reaches*
        // the cursor. Testing `first_seen` alone would hide a flow that began
        // earlier and was still running.
        if (since !== null && (edge.last_seen ?? 0) > since) return false;
        return byId.has(edge.source) && byId.has(edge.target);
      }),
    [edges, hidden, since, byId],
  );

  /** Full text for every flow, whether or not its label is drawn. */
  const textOf = useCallback(
    (edge: GraphEdge) =>
      `[${when(edge.first_seen)}] ${human(edge.total_raw, edge.decimals)} ${
        edge.symbol || "native"
      } · ${edge.transfers} transfer${edge.transfers === 1 ? "" : "s"}`,
    [],
  );

  /**
   * The same flow, short enough to sit between two columns.
   *
   * Drops "· 1 transfer", which is the common case and says nothing the amount
   * beside it does not: an aggregated edge is worth counting only when it
   * aggregates. The count stays in the hover text for every edge either way.
   */
  const shortTextOf = useCallback(
    (edge: GraphEdge) => {
      const head = `[${when(edge.first_seen)}] ${human(edge.total_raw, edge.decimals)} ${
        edge.symbol || "native"
      }`;
      return edge.transfers > 1 ? `${head} · ${edge.transfers} transfers` : head;
    },
    [],
  );

  const { labels, unlabelled } = useMemo(() => {
    const key = (edge: GraphEdge) => `${edge.source}>${edge.target}`;
    let chosen: GraphEdge[];
    if (labelMode === "none") {
      chosen = [];
    } else if (labelMode === "all") {
      chosen = visible;
    } else if (lit.size) {
      // Something is selected, so the reader has already said which flows
      // they mean. Labelling those and nothing else is the whole answer.
      chosen = visible.filter((edge) => lit.has(key(edge)));
    } else {
      chosen = [...visible]
        .sort((p, q) => q.transfers - p.transfers || (q.last_seen ?? 0) - (p.last_seen ?? 0))
        .slice(0, LABEL_BUDGET);
    }

    const drawn = new Set(chosen.map(key));
    const raw: EdgeLabel[] = chosen.map((edge, i) => {
      const mid = edgeMid(byId.get(edge.source)!, byId.get(edge.target)!);
      return {
        x: mid.x,
        y: mid.y - 5,
        lit: lit.has(key(edge)),
        index: i + 1,
        anchor: "middle" as const,
        text: shortTextOf(edge),
      };
    });
    return {
      labels: placeLabels(raw),
      unlabelled: visible.filter((edge) => !drawn.has(key(edge))).length,
    };
  }, [visible, byId, lit, labelMode, shortTextOf]);

  const onWheel = useCallback((event: React.WheelEvent) => {
    event.preventDefault();
    setView((v) => ({ ...v, zoom: Math.min(3, Math.max(0.15, v.zoom * (event.deltaY < 0 ? 1.1 : 0.9))) }));
  }, []);

  const onDown = useCallback((event: React.MouseEvent) => {
    dragging.current = { x: event.clientX, y: event.clientY };
  }, []);

  const onMove = useCallback((event: React.MouseEvent) => {
    const from = dragging.current;
    if (!from) return;
    const dx = event.clientX - from.x;
    const dy = event.clientY - from.y;
    dragging.current = { x: event.clientX, y: event.clientY };
    setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
  }, []);

  const stopDrag = useCallback(() => {
    dragging.current = null;
  }, []);

  /**
   * Scale and centre so the whole case is on screen.
   *
   * The drawing opened at 100% with the origin in the corner, which for
   * anything past a dozen addresses meant the first thing a reader saw was the
   * top-left quarter of it --- and nothing on screen said there was more. Every
   * tool in this space fits on open for that reason.
   *
   * Capped at 1: scaling a four-node case up to fill a monitor makes a small
   * case look like a large one.
   */
  const fit = useCallback(() => {
    const box = frame.current?.getBoundingClientRect();
    if (!box || !width || !height) return;
    const PAD = 28;
    const zoom = Math.max(
      0.15,
      Math.min(1, (box.width - PAD * 2) / width, (box.height - PAD * 2) / height),
    );
    setView({
      x: (box.width - width * zoom) / 2,
      y: (box.height - height * zoom) / 2,
      zoom,
    });
  }, [width, height]);

  // On the case changing, not on every redraw: refitting while somebody is
  // panning would drag the picture out from under them.
  const shape = `${nodes.length}:${Math.round(width)}:${Math.round(height)}`;
  const lastShape = useRef("");
  useEffect(() => {
    if (lastShape.current === shape) return;
    lastShape.current = shape;
    fit();
  }, [shape, fit]);

  return (
    <div className="canvas" ref={frame}>
      <svg
        ref={svgRef}
        data-width={width}
        data-height={height}
        onWheel={onWheel}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={stopDrag}
        onMouseLeave={stopDrag}
        role="img"
        aria-label={`fund flow graph, ${nodes.length} addresses, ${visible.length} flows`}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L8,4 L0,8z" className="arrow" />
          </marker>
          <marker id="arrowlit" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L8,4 L0,8z" className="arrow lit" />
          </marker>
        </defs>

        <g transform={`translate(${view.x},${view.y}) scale(${view.zoom})`}>
          {watermark ? (
            <text className="wm" x={width / 2} y={height / 2} textAnchor="middle">
              {watermark}
            </text>
          ) : null}

          {visible.map((edge) => {
            const a = byId.get(edge.source)!;
            const b = byId.get(edge.target)!;
            const on = lit.has(`${edge.source}>${edge.target}`);
            const d = edgePath(a, b);
            return (
              <g key={`${edge.source}>${edge.target}>${edge.asset}`}>
                {/* A 1.2px stroke is not a hover target. This invisible one is
                    wide enough to hit, and carries the text every flow keeps
                    whether or not its label is drawn --- suppressing a label to
                    keep the picture readable is a display decision, and it must
                    not remove the fact. */}
                <path className="edgehit" d={d}>
                  <title>
                    {short(a.as_written || a.address)} → {short(b.as_written || b.address)}
                    {"\n"}
                    {textOf(edge)}
                  </title>
                </path>
                <path
                  className={on ? "edge lit" : "edge"}
                  d={d}
                  markerEnd={`url(#${on ? "arrowlit" : "arrow"})`}
                />
              </g>
            );
          })}

          {labels.map((label) => (
            <text
              key={`${label.index}-${label.x}-${label.y}`}
              className={label.lit ? "elabel lit" : "elabel"}
              x={label.x}
              y={label.y}
              textAnchor={label.anchor}
            >
              <tspan className="eidx">[{label.index}]</tspan> {label.text}
            </text>
          ))}

          {placed.map((node) => {
            const classes = ["card"];
            if (node.address === selected) classes.push("on");
            // Dashed, because "we stopped looking here" and "the money stopped
            // here" are opposite claims and must not share a shape.
            if (node.frontier) classes.push("frontier");
            if (node.seed) classes.push("seed");
            return (
              <g
                key={node.address}
                className={classes.join(" ")}
                transform={`translate(${node.x},${node.y})`}
                onClick={() => onSelect(node.address)}
                // Selecting an address is the primary interaction here, and
                // it was reachable only with a mouse: 27 nodes, none in the
                // tab order. The roster list gave a keyboard path to the same
                // action, but a graph nobody can enter is not an accessible
                // graph --- it is a picture with a workaround.
                tabIndex={0}
                role="button"
                aria-label={`${node.label || (node.seed ? "seed" : "unlabelled")}, ${
                  node.as_written || node.address
                }${node.frontier ? ", frontier — its counterparties were never fetched" : ""}`}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(node.address);
                  }
                }}
              >
                <rect width={CARD_W} height={CARD_H} rx={0} />
                {/* Identity first. When a name is known it is the headline and
                    the address confirms it; when none is, the address *is* the
                    identity and the word "unlabelled" was taking the headline
                    on twenty boxes at once, which made a screen of distinct
                    addresses read as a screen of the same thing. */}
                {node.label ? (
                  <>
                    <text className="name" x={10} y={19}>
                      {node.label}
                    </text>
                    <text className="addr" x={10} y={36}>
                      {short(node.as_written || node.address)}
                    </text>
                  </>
                ) : (
                  <>
                    <text className="name addr-lead" x={10} y={20}>
                      {short(node.as_written || node.address)}
                    </text>
                    <text className="addr quiet" x={10} y={36}>
                      {node.seed ? "seed · no label" : "no label in any source"}
                    </text>
                  </>
                )}
                {node.category && node.category !== "unknown" ? (
                  <text className="cat" x={CARD_W - 10} y={19} textAnchor="end">
                    {node.category}
                  </text>
                ) : null}
                {node.frontier ? (
                  <title>
                    Frontier — its counterparties were never fetched. The picture stops
                    here because nobody looked, not because the money did.
                  </title>
                ) : null}
              </g>
            );
          })}
        </g>
      </svg>

      {/* Every shape in the drawing carries a claim --- dashed means nobody
          looked past it, which is the opposite of the money stopping --- and
          until now the only place that was written down was a source comment.
          A picture whose encoding is undocumented is a picture that gets read
          wrong, confidently. */}
      <div className={key ? "key open" : "key"}>
        <button onClick={() => setKey((k) => !k)} aria-expanded={key}>
          {key ? "× key" : "key"}
        </button>
        {key ? (
          <dl>
            <dt><span className="sw solid" /></dt>
            <dd>fetched — its counterparties were looked up</dd>
            <dt><span className="sw dashed" /></dt>
            <dd>
              <b>frontier</b> — nobody looked past it. The picture stops here;
              the money may not have.
            </dd>
            <dt><span className="sw seed" /></dt>
            <dd>seed — where you started</dd>
            <dt><span className="ln" /></dt>
            <dd>a flow, in the direction of the arrow</dd>
            <dt><span className="ln lit" /></dt>
            <dd>
              upstream of the selection — everything that reached it, by every
              path, not the shortest
            </dd>
            <dt className="wide" />
            <dd className="wide">
              <b>Left of the seed paid it; right of it was paid.</b> Columns are{" "}
              <b>hops</b>, not time — a box further out is further from the seed,
              not later.
            </dd>
          </dl>
        ) : null}
      </div>

      <div className="labelbar">
        <span className="quiet">labels</span>
        {(["auto", "all", "none"] as LabelMode[]).map((mode) => (
          <button
            key={mode}
            className={labelMode === mode ? "on" : ""}
            aria-pressed={labelMode === mode}
            onClick={() => setLabelMode(mode)}
            title={
              mode === "auto"
                ? `the ${LABEL_BUDGET} flows with the most transfers, or the selected path when there is one`
                : mode === "all"
                  ? "every flow, overlaps included"
                  : "none — hover a flow to read it"
            }
          >
            {mode}
          </button>
        ))}
        {unlabelled ? (
          // Named, not merely absent: an unlabelled flow on screen would
          // otherwise be indistinguishable from one that carries no amount.
          <span className="quiet">
            {unlabelled} more not labelled here — hover to read
          </span>
        ) : null}
      </div>

      <div className="zoombar">
        <button onClick={fit} title="fit the whole case on screen" aria-label="fit the whole case on screen">
          ⌖
        </button>
        {/* "minus" is what a screen reader reads off a bare glyph. */}
        <button
          aria-label="zoom out"
          onClick={() => setView((v) => ({ ...v, zoom: Math.max(0.15, v.zoom * 0.85) }))}
        >
          −
        </button>
        <span className="mono">{Math.round(view.zoom * 100)}%</span>
        <button
          aria-label="zoom in"
          onClick={() => setView((v) => ({ ...v, zoom: Math.min(3, v.zoom * 1.15) }))}
        >
          +
        </button>
      </div>
    </div>
  );
}
