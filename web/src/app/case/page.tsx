"use client";

/**
 * The case view: type an address, read the flow, click through it.
 *
 * Three columns, the arrangement the tools in this space converged on because
 * it fits the work: what you have looked at on the left, the shape of the money
 * in the middle, everything known about the selection on the right.
 *
 * **Reading never fetches.** The search box opens an address already in the
 * case, and when it is not there the answer says the store has never seen it
 * rather than drawing an empty graph — those look identical and mean opposite
 * things. The one control that reaches a chain is "follow the money from here",
 * which is a button somebody presses, not something a redraw can trigger.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { AskDialog } from "@/components/ask-dialog";
import { Graph } from "@/components/graph";
import { Selected } from "@/components/selected";
import { api, type Asset, type GraphReply } from "@/lib/api";
import { short } from "@/lib/layout";

const CHAINS = [
  ["1", "Ethereum"],
  ["56", "BSC"],
  ["137", "Polygon"],
  ["8453", "Base"],
  ["42161", "Arbitrum"],
] as const;

export default function CasePage() {
  const [address, setAddress] = useState("");
  const [chain, setChain] = useState("1");
  const [graph, setGraph] = useState<GraphReply | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [since, setSince] = useState<number | null>(null);
  const [spacing, setSpacing] = useState(1);
  const [watermark, setWatermark] = useState("");
  const [status, setStatus] = useState<{ text: string; tone: string }>({
    text: "",
    tone: "",
  });
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);

  const say = useCallback((text: string, tone = "") => setStatus({ text, tone }), []);

  const load = useCallback(
    async (target: string) => {
      if (!target) return;
      setBusy(true);
      try {
        const reply = await api<GraphReply>("/graph", {
          address: target,
          chain: `eip155:${chain}`,
        });
        setGraph(reply);
        setSelected(reply.nodes.find((n) => n.seed)?.address ?? null);
        setAddress(target);
        // The truncation flag is repeated in the status bar rather than left in
        // the payload: a graph that stopped at a limit and one that reached the
        // end of the money are the same picture.
        say(
          `${reply.nodes.length} addresses · ${reply.edges.length} flows` +
            (reply.truncated ? " · TRUNCATED — a limit stopped the walk" : ""),
          reply.truncated ? "warn" : "ok",
        );
      } catch (err) {
        setGraph(null);
        say((err as Error).message, "bad");
      } finally {
        setBusy(false);
      }
    },
    [chain, say],
  );

  // Restore from the URL so a link reopens the same view. `share` writes these.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const a = params.get("a");
    const c = params.get("c");
    if (c) setChain(c);
    if (a) void load(a);
    // Intentionally once: this is restoring an entry point, not tracking state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const assets = useMemo(() => {
    const groups = new Map<string, Asset[]>();
    for (const asset of graph?.assets ?? []) {
      const list = groups.get(asset.verdict);
      if (list) list.push(asset);
      else groups.set(asset.verdict, [asset]);
    }
    return groups;
  }, [graph]);

  const timeRange = useMemo(() => {
    const stamps = (graph?.edges ?? [])
      .map((e) => e.last_seen)
      .filter((s): s is number => typeof s === "number" && Number.isFinite(s));
    if (stamps.length < 2) return null;
    return { min: Math.min(...stamps), max: Math.max(...stamps) };
  }, [graph]);

  function toggleAsset(key: string) {
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function share() {
    const url = new URL(window.location.href);
    url.searchParams.set("a", address);
    url.searchParams.set("c", chain);
    void navigator.clipboard.writeText(url.toString());
    say("link copied — it restores this address and chain, not your unsaved edits", "ok");
  }

  return (
    <div className="case">
      <header className="case-bar">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void load(address.trim());
          }}
        >
          <input
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            placeholder="address already in the case"
            spellCheck={false}
            autoComplete="off"
            className="mono"
          />
          <select value={chain} onChange={(event) => setChain(event.target.value)}>
            {CHAINS.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
          <button type="submit" disabled={busy}>
            {busy ? "opening…" : "open"}
          </button>
        </form>
        <button onClick={() => setAsking(true)}>ask</button>
        <button onClick={share} disabled={!graph}>
          share
        </button>
        <label className="spacing" title="spread the columns out">
          <input
            type="range"
            min={0.5}
            max={2}
            step={0.1}
            value={spacing}
            onChange={(event) => setSpacing(Number(event.target.value))}
          />
        </label>
        <input
          className="wm"
          value={watermark}
          onChange={(event) => setWatermark(event.target.value)}
          placeholder="watermark"
        />
      </header>

      <main className="case-main">
        <aside>
          <h2>assets</h2>
          {assets.size === 0 ? <p className="muted small">nothing loaded</p> : null}
          {[...assets.entries()].map(([verdict, list]) => (
            <div key={verdict}>
              <p className={verdict === "GENUINE" ? "vgroup" : "vgroup bad"}>
                {verdict.toLowerCase()} {list.length}
              </p>
              <p className="vwhy">{list[0]?.why}</p>
              {list.map((asset) => (
                <label className="asset" key={asset.asset || asset.symbol}>
                  <input
                    type="checkbox"
                    checked={!hidden.has(asset.asset || "")}
                    onChange={() => toggleAsset(asset.asset || "")}
                  />
                  <span>{asset.symbol || "native"}</span>
                  <span className="muted mono">{asset.transfers}</span>
                </label>
              ))}
            </div>
          ))}

          <h2>addresses in view</h2>
          <div className="roster">
            {(graph?.nodes ?? []).map((node) => (
              <button
                key={node.address}
                className={node.address === selected ? "roster-row on" : "roster-row"}
                onClick={() => setSelected(node.address)}
              >
                {short(node.as_written || node.address)}
              </button>
            ))}
          </div>
        </aside>

        {graph ? (
          <Graph
            nodes={graph.nodes}
            edges={graph.edges}
            selected={selected}
            onSelect={setSelected}
            hidden={hidden}
            since={since}
            spacing={spacing}
            watermark={watermark}
          />
        ) : (
          <div className="canvas empty">
            <div>
              <h2 className="section">Nothing open</h2>
              <p className="lede">
                Type an address that is already in this case. Reading never reaches
                the network; only <em>follow the money from here</em> does, and only
                when you press it.
              </p>
            </div>
          </div>
        )}

        <Selected
          address={selected}
          chain={`eip155:${chain}`}
          onReload={() => load(address)}
          say={say}
        />
      </main>

      {timeRange ? (
        <div className="timebar">
          <span className="muted">up to</span>
          <input
            type="range"
            min={timeRange.min}
            max={timeRange.max}
            value={since ?? timeRange.max}
            onChange={(event) => setSince(Number(event.target.value))}
          />
          <span className="muted mono">
            {since === null || since >= timeRange.max
              ? "all"
              : new Date(since * 1000).toISOString().slice(0, 10)}
          </span>
        </div>
      ) : null}

      <footer className={`case-status ${status.tone}`}>{status.text}</footer>

      {asking ? (
        <AskDialog
          chain={`eip155:${chain}`}
          onClose={() => setAsking(false)}
          onRun={(plan) => {
            setAsking(false);
            const target = plan.params.address;
            if (target) void load(target);
            else say(plan.reading, "ok");
          }}
        />
      ) : null}
    </div>
  );
}
