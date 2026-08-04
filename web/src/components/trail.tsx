"use client";

/**
 * The path, for a reader who cannot find it in the graph.
 *
 * The graph draws every edge because every edge exists, and on a poisoned
 * address that is the wrong picture: the LpdFi attacker had 85 transfers and
 * six that mattered. This is the six — in and out, oldest first, with what was
 * folded and what was set aside stated underneath rather than quietly missing.
 *
 * **Funding is shown first and given equal weight.** Where money went is the
 * obvious question and where it came from is frequently the more useful one:
 * in that case the attacker was staked two thousand blocks before the exploit,
 * and every trace that began at the exploit block missed it.
 *
 * The folded rows are one click away, never gone. An amount engineered to
 * resemble the real one — 0.0000689 USDC against a real 689,429.79, digits
 * chosen to match — is evidence of who was targeted, and a view that deleted it
 * would be hiding the thing that identifies the campaign.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { short } from "@/lib/layout";

type Step = {
  direction: "in" | "out";
  counterparty: string;
  amount: string;
  symbol: string;
  block: number | null;
  at: string | null;
  tx: string;
  minor: boolean;
};

type Reply = {
  address: string;
  expanded: boolean;
  summary: string;
  considered: number;
  steps: Step[];
  significant: Step[];
  set_aside: Record<string, number>;
  forged_assets: string[];
};

function money(step: Step): string {
  const n = Number(step.amount);
  if (!Number.isFinite(n)) return step.amount;
  // Six places, because the dust that matters here is 0.0000689 and rounding
  // it to two would print 0.00 for the one row a reader is trying to identify.
  return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

function Row({ step }: { step: Step }) {
  return (
    <tr className={step.minor ? "minor" : undefined}>
      <td className="mono quiet">{step.block ?? "—"}</td>
      <td className="mono num">{money(step)}</td>
      <td className="mono sym">{step.symbol || "?"}</td>
      <td className="mono quiet">{step.direction === "in" ? "←" : "→"}</td>
      <td className="mono">{short(step.counterparty)}</td>
    </tr>
  );
}

export function Trail({ address, chain }: { address: string | null; chain: string }) {
  const [reply, setReply] = useState<Reply | null>(null);
  const [showFolded, setShowFolded] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!address) {
      setReply(null);
      return;
    }
    try {
      setError("");
      setReply(await api<Reply>("/trail", { address, chain }));
    } catch (err) {
      setReply(null);
      setError((err as Error).message);
    }
  }, [address, chain]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!address) return null;
  if (error) return <p className="note small">the path could not be built: {error}</p>;
  if (!reply) return null;

  const shown = showFolded ? reply.steps : reply.significant;
  const funding = shown.filter((s) => s.direction === "in");
  const onward = shown.filter((s) => s.direction === "out");
  const folded = reply.steps.length - reply.significant.length;

  if (!reply.considered) {
    return <p className="note small">Nothing in this case touches that address yet.</p>;
  }

  return (
    <div className="trail">
      {funding.length ? (
        <>
          <h3>funded by</h3>
          <table>
            <tbody>
              {funding.map((s) => (
                <Row key={`${s.tx}-${s.block}-in`} step={s} />
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="note small">Nothing material came in.</p>
      )}

      {onward.length ? (
        <>
          <h3>paid out to</h3>
          <table>
            <tbody>
              {onward.map((s) => (
                <Row key={`${s.tx}-${s.block}-out`} step={s} />
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="note small">Nothing material went out.</p>
      )}

      {/* Stated, not implied. A path that silently became legible is one
          somebody trusts for the wrong reason --- and one built from an
          address nobody fetched is a fragment wearing a history's shape. */}
      <p className={reply.expanded ? "note small" : "cannot small"}>{reply.summary}</p>
      {folded ? (
        <button className="fold" onClick={() => setShowFolded((v) => !v)}>
          {showFolded ? `hide the ${folded} folded` : `show the ${folded} folded`}
        </button>
      ) : null}
    </div>
  );
}
