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
 */

import { useCallback, useMemo, useRef, useState } from "react";

import type { GraphEdge, GraphNode } from "@/lib/api";
import {
  CARD_H,
  CARD_W,
  type EdgeLabel,
  edgePath,
  human,
  layout,
  litEdges,
  placeLabels,
  short,
  when,
} from "@/lib/layout";

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
}: Props) {
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 });
  const dragging = useRef<{ x: number; y: number } | null>(null);

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

  const labels = useMemo(() => {
    const raw: EdgeLabel[] = visible.map((edge, i) => {
      const target = byId.get(edge.target)!;
      return {
        x: target.x - 12,
        y: target.y + CARD_H / 2 - 6,
        lit: lit.has(`${edge.source}>${edge.target}`),
        index: i + 1,
        anchor: "end" as const,
        text: `[${when(edge.first_seen)}] ${human(edge.total_raw, edge.decimals)} ${
          edge.symbol || "native"
        }`,
      };
    });
    return placeLabels(raw);
  }, [visible, byId, lit]);

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

  return (
    <div className="canvas">
      <svg
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
            return (
              <path
                key={`${edge.source}>${edge.target}>${edge.asset}`}
                className={on ? "edge lit" : "edge"}
                d={edgePath(a, b)}
                markerEnd={`url(#${on ? "arrowlit" : "arrow"})`}
              />
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
                <text className="name" x={10} y={19}>
                  {node.label || (node.seed ? "seed" : "unlabelled")}
                </text>
                <text className="addr" x={10} y={36}>
                  {short(node.as_written || node.address)}
                </text>
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

      <div className="zoombar">
        <button onClick={() => setView({ x: 0, y: 0, zoom: 1 })} title="reset the view">
          ⌖
        </button>
        <button onClick={() => setView((v) => ({ ...v, zoom: Math.max(0.15, v.zoom * 0.85) }))}>
          −
        </button>
        <span className="mono">{Math.round(view.zoom * 100)}%</span>
        <button onClick={() => setView((v) => ({ ...v, zoom: Math.min(3, v.zoom * 1.15) }))}>
          +
        </button>
      </div>
    </div>
  );
}
