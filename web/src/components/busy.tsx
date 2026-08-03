"use client";

/**
 * That something is happening, and roughly how much of it.
 *
 * Two shapes, because the two waits are different in kind:
 *
 *   * **indeterminate** — a single request whose duration nobody knows. A bar
 *     that animates without claiming a position, because a fake percentage
 *     that jumps to 90% and sits there is a lie about progress.
 *   * **counted** — a batch where the work is countable, so it says "3 of 12"
 *     and fills to match. Expanding a frontier of twenty addresses is a real
 *     wait, and "fetching…" for forty seconds is indistinguishable from a hang.
 *
 * Deliberately thin and at the top edge. This sits above a graph somebody is
 * reading; a spinner in the middle of the canvas would cover the thing they
 * are waiting to see.
 */

type Props = {
  /** Nothing renders when false, so callers can mount this unconditionally. */
  on: boolean;
  /** Optional counted progress. Omit for an unknown duration. */
  done?: number;
  total?: number;
  label?: string;
};

export function Busy({ on, done, total, label }: Props) {
  if (!on) return null;
  const counted = typeof done === "number" && typeof total === "number" && total > 0;
  const pct = counted ? Math.round((done! / total!) * 100) : 0;

  return (
    <div
      className="busy"
      role="progressbar"
      aria-label={label ?? "working"}
      aria-valuemin={0}
      aria-valuemax={counted ? total : undefined}
      aria-valuenow={counted ? done : undefined}
    >
      <div
        className={counted ? "busy-fill" : "busy-fill sliding"}
        style={counted ? { width: `${pct}%` } : undefined}
      />
      {label ? (
        <span className="busy-label">
          {label}
          {counted ? ` ${done}/${total}` : null}
        </span>
      ) : null}
    </div>
  );
}
