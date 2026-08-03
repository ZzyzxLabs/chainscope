"use client";

/**
 * Everything known about the selected address, and what can be done to it.
 *
 * The panel's job is to make three states visually different, because they
 * arrive as the same empty list and mean opposite things:
 *
 *   * claims were found;
 *   * every source answered and none named it;
 *   * a source could not be read, so this is not a clean result.
 *
 * The third is the one this tool exists to keep visible. It is rendered in the
 * caution colour with the failing sources named, never as the grey
 * "unlabelled" the second state gets.
 */

import { useCallback, useEffect, useState } from "react";

import { Spinner } from "@/components/spinner";
import { api, boot, type ExpandReply, type ResolveReply } from "@/lib/api";

type Props = {
  address: string | null;
  chain: string;
  /** Addresses whose counterparties were never fetched. */
  frontier: string[];
  /** An analyzer the reader arrived asking for, from `/case/?run=`. */
  highlight?: string | null;
  /** Report what is in flight, so the page can show a bar. */
  onWork?: (state: { on: boolean; done?: number; total?: number; label?: string }) => void;
  onReload: () => void;
  say: (text: string, tone?: string) => void;
};

type Registered = {
  name: string;
  description: string;
  /** False when this analysis cannot work on this chain at all. */
  applies: boolean;
  /** Parameters it needs beyond an address. */
  needs: string[];
};

export function Selected({
  address,
  chain,
  frontier,
  highlight,
  onWork,
  onReload,
  say,
}: Props) {
  const [found, setFound] = useState<ResolveReply | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [out, setOut] = useState(true);
  const [into, setInto] = useState(true);
  const [window_, setWindow] = useState("");
  // Asked for, never hard-coded. The page offered three of thirteen because the
  // list lived in a literal here; the server knows what is actually installed.
  const [registered, setRegistered] = useState<Registered[]>([]);
  const [result, setResult] = useState<string>("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const writable = boot().writable;

  useEffect(() => {
    api<{ analyses: Registered[] }>("/analyses", { chain })
      .then((reply) => setRegistered(reply.analyses))
      .catch(() => setRegistered([]));
  }, [chain]);

  const run = useCallback(
    async (item: Registered) => {
      onWork?.({ on: true, label: item.name });
      const missing = item.needs.filter((n) => !params[n]?.trim());
      if (missing.length) {
        // Asked for, not guessed. Running with a blank required parameter
        // would return an error naming what should have been typed, which is a
        // worse way to learn it than a field.
        setOpen(item.name);
        say(`${item.name} needs ${missing.join(" and ")}`, "warn");
        onWork?.({ on: false });
        return;
      }
      try {
        const extra = Object.fromEntries(item.needs.map((n) => [n, params[n]]));
        const r = await api<{
          findings: { title?: string; summary?: string }[];
          hypotheses?: unknown[];
        }>("/analyze", { name: item.name, address: address ?? "", chain, ...extra });
        // Findings and hypotheses counted separately: one is observed, the
        // other inferred, and a single number merges them into a claim nobody
        // made.
        const parts = [`${r.findings.length} finding(s)`];
        if (r.hypotheses?.length) parts.push(`${r.hypotheses.length} hypothesis(es)`);
        say(`${item.name}: ${parts.join(", ")}`, "ok");
        setResult(
          r.findings.length || r.hypotheses?.length
            ? r.findings.map((f) => `• ${f.title ?? f.summary ?? JSON.stringify(f)}`).join("\n")
            : `${item.name} found nothing. That is not a clean result — it means ` +
              `this pattern is not present in what has been fetched so far.`,
        );
      } catch (err) {
        say(`${item.name}: ${(err as Error).message}`, "bad");
        setResult((err as Error).message);
      } finally {
        onWork?.({ on: false });
      }
    },
    [address, chain, params, say, onWork],
  );

  useEffect(() => {
    if (!address) {
      setFound(null);
      return;
    }
    let live = true;
    api<ResolveReply>("/resolve", { address, chain })
      .then((reply) => {
        if (live) setFound(reply);
      })
      .catch(() => {
        if (live) setFound(null);
      });
    return () => {
      live = false;
    };
  }, [address, chain]);

  const expand = useCallback(
    async (targets: string[]) => {
    if (!targets.length) return;
    const ways = [out && "out", into && "in"].filter(Boolean).join(",");
    if (!ways) {
      say("pick a direction — in, out, or both", "bad");
      return;
    }
    setExpanding(true);
    // Counted when there is more than one address: the reader can see it
    // moving, and a stalled batch looks different from a slow one.
    onWork?.({
      on: true,
      total: targets.length > 1 ? targets.length : undefined,
      done: targets.length > 1 ? 0 : undefined,
      label: targets.length > 1 ? "expanding" : "fetching one hop",
    });
    try {
      // The relative window resolves here, against the reader's clock, and
      // travels as an absolute instant. If the server interpreted "last 7 days"
      // the same request replayed tomorrow would mean a different week, and a
      // case that cannot be replayed is not evidence.
      const since = window_
        ? Math.floor(Date.now() / 1000) - Number(window_)
        : undefined;
      const reply = await api<ExpandReply>("/expand", {
        address: targets.join(","),
        chain,
        direction: ways,
        since,
      });
      const bits = [
        `${reply.fetched} transfer(s) fetched`,
        `${reply.new_addresses.length} new address(es)`,
      ];
      // Per address, because a total says nothing about which of ten failed.
      if (reply.failed?.length) {
        bits.push(`${reply.failed.length} address(es) could not be fetched`);
      }
      // What it did NOT bring back. A filter that matched nothing and an
      // address that never moved money leave the same smaller graph.
      if (reply.filtered_out) bits.push(`${reply.filtered_out} flow(s) excluded by your filter`);
      if (reply.truncated) bits.push("the list was cut short");
      if (!reply.complete) bits.push("the provider had more — this is not all of it");
      say(bits.join(" · "), reply.complete && !reply.truncated ? "ok" : "warn");
      onReload();
    } catch (err) {
      say(`expand failed: ${(err as Error).message}`, "bad");
    } finally {
      setExpanding(false);
      onWork?.({ on: false });
    }
    },
    [chain, out, into, window_, say, onReload, onWork],
  );

  if (!address) {
    return (
      <aside className="right">
        <h2>selected</h2>
        <p className="muted small">Nothing selected. Click an address in the graph.</p>
      </aside>
    );
  }

  const claim = found?.claims[0];
  const unreliable = found?.reliable === false;

  return (
    <aside className="right">
      <h2>selected</h2>
      <dl className="kv">
        <dt>address</dt>
        <dd className="mono break">{address}</dd>
        <dt>label</dt>
        <dd>
          {claim ? (
            claim.label
          ) : unreliable ? (
            <span className="absent">unknown — a source failed, see below</span>
          ) : (
            <span className="muted">none — unlabelled, not unimportant</span>
          )}
        </dd>
        {claim ? (
          <>
            <dt>confidence</dt>
            <dd>{claim.confidence}</dd>
            <dt>source</dt>
            <dd className="small">{claim.source}</dd>
          </>
        ) : null}
      </dl>

      {unreliable ? (
        <p className="cannot">
          <b>Incomplete</b> {found?.unreachable_sources.join("; ")}. An empty result
          here is not a clean one.
        </p>
      ) : null}
      {found?.note && !unreliable ? <p className="note small">{found.note}</p> : null}

      <h2>follow the money from here</h2>
      <div className="ctl">
        <label>
          <input type="checkbox" checked={out} onChange={() => setOut(!out)} /> out
        </label>
        <label>
          <input type="checkbox" checked={into} onChange={() => setInto(!into)} /> in
        </label>
      </div>
      <div className="ctl">
        <select value={window_} onChange={(event) => setWindow(event.target.value)}>
          <option value="">any time</option>
          <option value="86400">last 24 hours</option>
          <option value="604800">last 7 days</option>
          <option value="2592000">last 30 days</option>
          <option value="7776000">last 90 days</option>
        </select>
      </div>
      <div className="ctl">
        <button onClick={() => expand(address ? [address] : [])} disabled={expanding}>
          {expanding ? <Spinner /> : null}
          {expanding ? "fetching" : "expand one hop"}
        </button>
      </div>
      {frontier.length > 1 ? (
        <div className="ctl">
          <button onClick={() => expand(frontier)} disabled={expanding}>
            {expanding ? <Spinner /> : null}
            {expanding ? "fetching" : `open the whole frontier (${frontier.length})`}
          </button>
        </div>
      ) : null}
      <p className="cannot">
        <b>This is the only control that reaches a chain.</b> A filter narrows what
        is fetched, so it narrows what you will ever see — the result says how many
        flows it excluded, because a window that misses the interesting transfer
        looks exactly like an address that never made one.
      </p>

      <h2>run on this address</h2>
      <div className="ctl wrap">
        {registered
          // A control that cannot work on this chain is worse than no control:
          // pressing it returns "no walker configured", and the reader cannot
          // tell a wrong chain from a real absence of the pattern.
          .filter((item) => item.applies)
          .map((item) => (
            <button
              key={item.name}
              title={item.description}
              className={item.name === highlight ? "wanted" : undefined}
              onClick={() => void run(item)}
            >
              {item.name}
              {item.needs.length ? <span className="needs">·{item.needs.length}</span> : null}
            </button>
          ))}
      </div>

      {open ? (
        <div className="params">
          {(registered.find((r) => r.name === open)?.needs ?? []).map((need) => (
            <input
              key={need}
              className="mono"
              placeholder={need}
              value={params[need] ?? ""}
              onChange={(event) =>
                setParams((current) => ({ ...current, [need]: event.target.value }))
              }
            />
          ))}
          <button
            onClick={() => {
              const item = registered.find((r) => r.name === open);
              if (item) void run(item);
            }}
          >
            run {open}
          </button>
        </div>
      ) : null}

      {registered.some((item) => !item.applies) ? (
        <p className="note small">
          {registered.filter((i) => !i.applies).length} analysis(es) are installed
          but do not apply to this chain:{" "}
          {registered
            .filter((i) => !i.applies)
            .map((i) => i.name)
            .join(", ")}
          . Hidden rather than offered, because a button that can only answer
          &ldquo;not configured for this chain&rdquo; reads like an absence of
          the pattern.
        </p>
      ) : null}
      {result ? <pre className="result">{result}</pre> : null}

      {!writable ? (
        <p className="note small">
          Read-only. Start the server with <code>--writable</code> to record labels
          and notes from this page — writing to a case should be a decision, not the
          default for a command you ran to look at something.
        </p>
      ) : null}
    </aside>
  );
}
