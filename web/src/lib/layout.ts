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
 * Room to the left of the first column, for its labels.
 *
 * Labels sit to the left of the node they describe, which for the first column
 * means outside the drawing: every inbound edge into the seed had its amount
 * clipped to a fragment. Flipping those few to run rightward was worse --- they
 * then shared one anchor, and the de-collision pass stacked twenty of them into
 * a block. A gutter keeps every label in the same place relative to its node,
 * which is what makes a column of them scannable.
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
  const stepX = CARD_W + 210 * spacing;
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
  const x1 = a.x + CARD_W;
  const y1 = a.y + CARD_H / 2;
  const x2 = b.x;
  const y2 = b.y + CARD_H / 2;
  const mid = x1 + Math.max(40, (x2 - x1) * 0.45);
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

/**
 * Every edge on a path from a seed to the selection — not the shortest one.
 *
 * A split that rejoins is the structure worth seeing, and a shortest-path
 * highlight hides exactly that.
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

export type EdgeLabel = {
  x: number;
  y: number;
  lit: boolean;
  text: string;
  index: number;
  /** Which side of the anchor the text runs. See `placeLabels`. */
  anchor: "start" | "end";
};

/**
 * Place the edge labels, then push apart any that would collide.
 *
 * Anchoring to the target's row stops twelve labels stacking at a shared
 * source, but two edges whose targets sit on the same row still land on the
 * same pixels. Grouping by anchor column and walking down keeps a nudge in one
 * part of the graph from cascading through an unrelated part.
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
    const column = Math.round(label.x);
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
