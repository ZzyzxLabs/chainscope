/**
 * Where the boxes and lines go.
 *
 * Kept as plain functions rather than a hook so the geometry can be tested
 * without a renderer, and so the rules below are stated once in a place a
 * reviewer can read end to end.
 */

import type { GraphEdge, GraphNode } from "./api";

export const CARD_W = 218;
export const CARD_H = 52;
export const GAP_Y = 22;

/**
 * Room to the left of the first column.
 *
 * Held over from when labels sat to the left of the node they described and
 * the first column's ran off the drawing. `edgeMid` moved them onto their own
 * curves, so the gutter is no longer load-bearing for text --- it stays
 * because an inbound edge into the seed still needs somewhere to come from,
 * and a seed flush against the left edge reads as the start of the money
 * rather than the start of the picture.
 */
export const GUTTER = 300;

export type Placed = GraphNode & { x: number; y: number };

/**
 * Columns by hop depth, left to right.
 *
 * Deliberately not a force layout. Force arranges by connectivity, and then a
 * five-hop laundering chain and a five-way split look identical — the one
 * distinction a reader most needs is the one it destroys. Depth columns also
 * make the drawing stable: the same case opens the same way every time, so two
 * screenshots taken a week apart are comparable.
 *
 * The columns are hops, not time. An address two columns right is not
 * necessarily later, and the documentation says so next to this view.
 */
export function layout(
  nodes: GraphNode[],
  spacing: number,
): { placed: Placed[]; width: number; height: number } {
  const columns = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const column = columns.get(node.depth);
    if (column) column.push(node);
    else columns.set(node.depth, [node]);
  }

  const depths = [...columns.keys()].sort((a, b) => a - b);
  // Wide enough for a label to sit between two columns without lying across
  // the next one. A dated amount at this font is around 250px, and the gap was
  // 210 --- so every label in the middle of the graph overlapped a card, which
  // is how twenty-nine legible flows still read as a hairball.
  const stepX = CARD_W + 290 * spacing;
  const stepY = CARD_H + GAP_Y * spacing;

  const placed: Placed[] = [];
  let height = 0;
  depths.forEach((depth, i) => {
    const column = columns.get(depth)!;
    column.forEach((node, j) => {
      placed.push({ ...node, x: GUTTER + i * stepX, y: 30 + j * stepY });
    });
    height = Math.max(height, 30 + column.length * stepY);
  });

  return {
    placed,
    width: GUTTER + 40 + depths.length * stepX,
    height: Math.max(height, 200),
  };
}

/**
 * One turn out of the source's right edge into the target's left.
 *
 * Bundling out of a single point is what makes a fan of twelve payments legible
 * instead of twelve lines crossing the same space.
 */
export function edgePath(a: Placed, b: Placed): string {
  const { x1, y1, x2, y2, bend } = ends(a, b);
  return `M${x1},${y1} C${bend},${y1} ${bend},${y2} ${x2},${y2}`;
}

function ends(a: Placed, b: Placed) {
  const x1 = a.x + CARD_W;
  const y1 = a.y + CARD_H / 2;
  const x2 = b.x;
  const y2 = b.y + CARD_H / 2;
  return { x1, y1, x2, y2, bend: x1 + Math.max(40, (x2 - x1) * 0.45) };
}

/**
 * The point halfway along the curve, where that edge's label goes.
 *
 * Anchoring at the **target** was the previous rule and it stacked every
 * fan-in: fourteen flows into one address put fourteen labels on one anchor,
 * which the de-collision pass turned into a wall of text lying across the
 * node. Anchoring at the source, which that rule replaced, does the same to
 * every fan-out. Neither endpoint is distinct enough alone; the midpoint
 * differs whenever *either* end does, so a fan spreads along its own curves.
 *
 * Exact rather than eyeballed: for a cubic whose control points share the
 * endpoints' y, the t=0.5 point is ((x1 + x2 + 6·bend)/8, (y1 + y2)/2).
 */
export function edgeMid(a: Placed, b: Placed): { x: number; y: number } {
  const { x1, y1, x2, y2, bend } = ends(a, b);
  return { x: (x1 + x2 + 6 * bend) / 8, y: (y1 + y2) / 2 };
}

/**
 * Every edge upstream of the selection — the whole subgraph, not one path.
 *
 * A split that rejoins is the structure worth seeing, and a shortest-path
 * highlight hides exactly that.
 *
 * This said "on a path from a seed to the selection" and did not do that: it
 * walks back over inbound edges until it runs out, which reaches funders that
 * are on no path from any seed. With the seed itself selected --- the state
 * every case opens in --- the stated rule lights nothing and this lit 17 of 29
 * edges. The walk is the more useful of the two, so the sentence is corrected
 * to it rather than the other way round. Restricting to seed-reachable paths
 * would be an intersection with the seed's descendants, and would hide exactly
 * the addresses a reader most wants: the ones that paid in from outside.
 */
export function litEdges(edges: GraphEdge[], selected: string | null): Set<string> {
  const lit = new Set<string>();
  if (!selected) return lit;

  const back = new Map<string, GraphEdge[]>();
  for (const edge of edges) {
    const list = back.get(edge.target);
    if (list) list.push(edge);
    else back.set(edge.target, [edge]);
  }

  const queue = [selected];
  const seen = new Set([selected]);
  while (queue.length) {
    const at = queue.pop()!;
    for (const edge of back.get(at) ?? []) {
      lit.add(`${edge.source}>${edge.target}`);
      if (!seen.has(edge.source)) {
        seen.add(edge.source);
        queue.push(edge.source);
      }
    }
  }
  return lit;
}

/** Roughly the width of a label, and so the distance at which two can collide. */
export const LABEL_BUCKET = 200;

export type EdgeLabel = {
  x: number;
  y: number;
  lit: boolean;
  text: string;
  index: number;
  /** Which side of the anchor the text runs. See `placeLabels`. */
  anchor: "start" | "middle" | "end";
};

/**
 * Place the edge labels, then push apart any that would collide.
 *
 * `edgeMid` spreads a fan across its own curves, but two edges running roughly
 * parallel still put their midpoints on the same pixels. Grouping by anchor
 * column and walking down keeps a nudge in one part of the graph from
 * cascading through an unrelated part.
 *
 * Greedy rather than a force simulation, deliberately: this has to draw the
 * same picture every time. A label that wanders between redraws is worse than
 * one sitting slightly off its line, because the reader is comparing
 * screenshots taken minutes apart.
 */
export function placeLabels(labels: EdgeLabel[]): EdgeLabel[] {
  const LINE = 12;
  const columns = new Map<number, EdgeLabel[]>();
  for (const label of labels) {
    // Bucketed, not exact. Midpoint anchors land on arbitrary fractions, so
    // an exact key put two labels 1px apart in different groups and neither
    // ever saw the other --- the de-collision pass ran and the labels still
    // overlapped. The bucket is a text width, since that is the distance at
    // which two labels can actually collide.
    const column = Math.round(label.x / LABEL_BUCKET);
    const group = columns.get(column);
    if (group) group.push(label);
    else columns.set(column, [label]);
  }

  const out: EdgeLabel[] = [];
  for (const group of columns.values()) {
    group.sort((p, q) => p.y - q.y);
    let floor = -Infinity;
    for (const label of group) {
      const y = Math.max(label.y, floor);
      floor = y + LINE;
      out.push({ ...label, y });
    }
  }
  return out;
}

/**
 * A wei-scale amount as something a person reads.
 *
 * Takes the value as a string and divides with BigInt, because the input
 * routinely exceeds `Number.MAX_SAFE_INTEGER` and parsing it first would round
 * the number before it was ever displayed.
 */
export function human(raw: string, decimals: number): string {
  let value: bigint;
  try {
    value = BigInt(raw);
  } catch {
    return raw;
  }
  if (decimals <= 0) return value.toLocaleString("en-US");

  const scale = 10n ** BigInt(decimals);
  const whole = value / scale;
  const fraction = value % scale;
  if (fraction === 0n) return whole.toLocaleString("en-US");

  // Two significant places past the point is what fits the label; more makes
  // the fan of amounts unreadable and less hides a dust transfer entirely.
  const digits = fraction.toString().padStart(decimals, "0").slice(0, 2).replace(/0+$/, "");
  return digits ? `${whole.toLocaleString("en-US")}.${digits}` : whole.toLocaleString("en-US");
}

export function short(address: string): string {
  return address.length > 16 ? `${address.slice(0, 8)}…${address.slice(-6)}` : address;
}

export function when(seconds: number | null): string {
  if (!seconds) return "undated";
  return new Date(seconds * 1000).toISOString().slice(0, 16).replace("T", " ");
}
