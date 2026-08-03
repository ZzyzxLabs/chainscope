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

import { api, boot, type ExpandReply, type ResolveReply } from "@/lib/api";

type Props = {
  address: string | null;
  chain: string;
  onReload: () => void;
  say: (text: string, tone?: string) => void;
};

const ANALYZERS = ["impersonation", "poisoning", "contributors"] as const;

export function Selected({ address, chain, onReload, say }: Props) {
  const [found, setFound] = useState<ResolveReply | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [out, setOut] = useState(true);
  const [into, setInto] = useState(true);
  const [window_, setWindow] = useState("");
  const writable = boot().writable;

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

  const expand = useCallback(async () => {
    if (!address) return;
    const ways = [out && "out", into && "in"].filter(Boolean).join(",");
    if (!ways) {
      say("pick a direction — in, out, or both", "bad");
      return;
    }
    setExpanding(true);
    try {
      // The relative window resolves here, against the reader's clock, and
      // travels as an absolute instant. If the server interpreted "last 7 days"
      // the same request replayed tomorrow would mean a different week, and a
      // case that cannot be replayed is not evidence.
      const since = window_
        ? Math.floor(Date.now() / 1000) - Number(window_)
        : undefined;
      const reply = await api<ExpandReply>("/expand", {
        address,
        chain,
        direction: ways,
        since,
      });
      const bits = [
        `${reply.fetched} transfer(s) fetched`,
        `${reply.new_addresses.length} new address(es)`,
      ];
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
    }
  }, [address, chain, out, into, window_, say, onReload]);

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
        <button onClick={expand} disabled={expanding}>
          {expanding ? "fetching…" : "expand one hop"}
        </button>
      </div>
      <p className="cannot">
        <b>This is the only control that reaches a chain.</b> A filter narrows what
        is fetched, so it narrows what you will ever see — the result says how many
        flows it excluded, because a window that misses the interesting transfer
        looks exactly like an address that never made one.
      </p>

      <h2>run on this address</h2>
      <div className="ctl wrap">
        {ANALYZERS.map((name) => (
          <button
            key={name}
            onClick={() =>
              api<{ findings: unknown[] }>("/analyze", { name, address, chain })
                .then((r) => say(`${name}: ${r.findings.length} finding(s)`, "ok"))
                .catch((err) => say(`${name}: ${(err as Error).message}`, "bad"))
            }
          >
            {name}
          </button>
        ))}
      </div>

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
