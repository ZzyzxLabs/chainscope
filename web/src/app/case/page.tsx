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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Activity } from "@/components/activity";
import { AskDialog } from "@/components/ask-dialog";
import { Busy } from "@/components/busy";
import { Spinner } from "@/components/spinner";
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
  /** An address that was opened and holds nothing yet. Not the same as null. */
  const [unopened, setUnopened] = useState<string | null>(null);
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
  const [pending, setPending] = useState<string | null>(null);
  // Progress reported by the panel, so a batch expand of twenty addresses is a
  // count rather than forty seconds of "fetching…", which is indistinguishable
  // from a hang.
  const [work, setWork] = useState<{
    on: boolean;
    done?: number;
    total?: number;
    label?: string;
  }>({ on: false });

  const svgRef = useRef<SVGSVGElement | null>(null);

  /**
   * Undo for the view, not for the case.
   *
   * What it covers is what a reader changes while looking: which assets are
   * hidden, the spacing, the watermark. Deliberately NOT the case record ---
   * a label or a note is append-only and authored, and an undo button that
   * silently retracted one would be a worse thing than no undo at all.
   *
   * The inline page had this and the port lost it. Found by diffing the two
   * pages' controls, not by anyone missing it, which is how a rewrite loses
   * things: the absence reads as a decision.
   */
  const trail = useRef<{ past: string[]; future: string[] }>({ past: [], future: [] });
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  const view = useCallback(
    () => JSON.stringify({ hidden: [...hidden], spacing, watermark }),
    [hidden, spacing, watermark],
  );

  const applyView = useCallback((raw: string) => {
    const v = JSON.parse(raw) as { hidden: string[]; spacing: number; watermark: string };
    setHidden(new Set(v.hidden));
    setSpacing(v.spacing);
    setWatermark(v.watermark);
  }, []);

  // Record after the change lands, so `past` holds states the reader saw.
  useEffect(() => {
    const now = view();
    const t = trail.current;
    if (t.past[t.past.length - 1] === now) return;
    t.past.push(now);
    if (t.past.length > 60) t.past.shift();
    t.future.length = 0;
    setCanUndo(t.past.length > 1);
    setCanRedo(false);
  }, [view]);

  function undo() {
    const t = trail.current;
    if (t.past.length < 2) return;
    t.future.push(t.past.pop()!);
    applyView(t.past[t.past.length - 1]);
    setCanUndo(t.past.length > 1);
    setCanRedo(true);
  }

  function redo() {
    const t = trail.current;
    const next = t.future.pop();
    if (!next) return;
    t.past.push(next);
    applyView(next);
    setCanUndo(true);
    setCanRedo(t.future.length > 0);
  }
  const say = useCallback((text: string, tone = "") => setStatus({ text, tone }), []);

  /**
   * The drawing as a self-contained SVG file.
   *
   * Styles are inlined rather than linked, by the same rule as everything else
   * here: a picture that fetches a stylesheet to render is a picture that
   * stops rendering, and that tells somebody it was opened. The watermark is
   * inside the SVG for the same reason --- one that vanishes on export is
   * decoration.
   *
   * The pan and zoom are dropped and the whole graph written at its natural
   * size, because what somebody wants in a report is the case, not the corner
   * of it they happened to be looking at.
   */
  function exportSvg() {
    const live = svgRef.current;
    if (!live) {
      say("nothing to export yet", "warn");
      return;
    }
    const svg = live.cloneNode(true) as SVGSVGElement;
    svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const w = live.getAttribute("data-width");
    const h = live.getAttribute("data-height");
    if (w && h) {
      svg.setAttribute("width", w);
      svg.setAttribute("height", h);
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    }
    const g = svg.querySelector("g");
    if (g) g.setAttribute("transform", "translate(20,20)");

    const css = [...document.styleSheets]
      .flatMap((sheet) => {
        try {
          return [...sheet.cssRules].map((r) => r.cssText);
        } catch {
          // A cross-origin sheet cannot be read. There are none here by
          // design, but silently producing an unstyled file would be worse
          // than skipping it.
          return [];
        }
      })
      .filter((t) => /\.card|\.edge|\.elabel|\.wm|\.eidx|\.arrow/.test(t))
      .join("\n");
    const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
    style.textContent = css;
    svg.insertBefore(style, svg.firstChild);

    const blob = new Blob(
      ['<?xml version="1.0" encoding="UTF-8"?>\n', svg.outerHTML],
      { type: "image/svg+xml" },
    );
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${(address || "case").slice(0, 12)}.svg`;
    link.click();
    URL.revokeObjectURL(link.href);
    say("exported the graph as SVG — styles inlined, so it opens offline", "ok");
  }

  const load = useCallback(
    async (target: string, onChain?: string) => {
      if (!target) return;
      setBusy(true);
      // `onChain` overrides the state, because the one caller that needs it
      // sets the chain in the same effect that opens the address --- and a
      // `useCallback` closes over the render it was created in, so `chain` is
      // still whatever it was before `setChain` was called.
      //
      // The symptom was silent and total: every `?a=…&c=56` link queried
      // Ethereum, reported "no transfers found ... on eip155:1", and showed
      // BSC in the dropdown while doing it. Correct, honest, and about the
      // wrong chain.
      const on = onChain ?? chain;
      try {
        const reply = await api<GraphReply>("/graph", {
          address: target,
          chain: `eip155:${on}`,
        });
        // A graph with no nodes is a valid answer to "what do we hold about
        // this address" --- the answer being "nothing" --- and it is the state
        // every case starts in. Kept distinct from `graph === null`, which is
        // "you have not asked yet".
        setGraph(reply.nodes.length ? reply : null);
        setUnopened(reply.nodes.length ? null : target);
        setSelected(reply.nodes.find((n) => n.seed)?.address ?? null);
        setAddress(target);
        if (!reply.nodes.length) {
          say(`nothing fetched for ${short(target)} yet`, "warn");
          return;
        }
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
        // A case file that does not exist yet is not an error to report at the
        // reader; it is the state before the first fetch, and it has a button.
        const message = (err as Error).message;
        if (/no store at /.test(message)) {
          setUnopened(target);
          setAddress(target);
          say(`nothing fetched for ${short(target)} yet`, "warn");
        } else {
          say(message, "bad");
        }
      } finally {
        setBusy(false);
      }
    },
    [chain, say],
  );

  /**
   * The first fetch of a case, from the empty state.
   *
   * Deliberately the same endpoint the selected-node panel uses: there is one
   * way to reach the network from this page and it is `expand`, so the first
   * hop and the fiftieth cost the same, obey the same filters, and report the
   * same way. A separate "start a case" path would be a second network door
   * with its own rules to get wrong.
   */
  const seedFetch = useCallback(
    async (target: string) => {
      setBusy(true);
      try {
        await api("/expand", { address: target, chain: `eip155:${chain}`, direction: "both" });
        setUnopened(null);
        await load(target);
      } catch (err) {
        say(`fetch failed: ${(err as Error).message}`, "bad");
      } finally {
        setBusy(false);
      }
    },
    [chain, load, say],
  );

  // Restore from the URL so a link reopens the same view. `share` writes these.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const a = params.get("a");
    const c = params.get("c");
    // Arriving from the landing page's "run it" with no address yet: the
    // analyzer is remembered and offered once there is something to run it on.
    const run = params.get("run");
    if (c) setChain(c);
    if (run) setPending(run);
    // `c` passed explicitly: `setChain` above does not reach `load`'s closure
    // until the next render.
    if (a) void load(a, c ?? undefined);
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
            placeholder="address"
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
            {busy ? <Spinner /> : null}
            {busy ? "opening" : "open"}
          </button>
        </form>
        <button onClick={() => setAsking(true)}>ask</button>
        <button onClick={share} disabled={!graph}>
          share
        </button>
        <button onClick={exportSvg} disabled={!graph} title="download the graph as SVG">
          export
        </button>
        <button onClick={undo} disabled={!canUndo} title="undo a view change">
          undo
        </button>
        <button onClick={redo} disabled={!canRedo} title="redo a view change">
          redo
        </button>
        <label className="spacing" title="spread the columns out">
          <input
            type="range"
            aria-label="column spacing"
            min={0.5}
            max={2}
            step={0.1}
            value={spacing}
            onChange={(event) => setSpacing(Number(event.target.value))}
          />
        </label>
        <input
          className="wm"
          aria-label="watermark drawn across the graph"
          value={watermark}
          onChange={(event) => setWatermark(event.target.value)}
          placeholder="watermark"
        />
      </header>

      {/* Under the toolbar rather than over the canvas: this is the one place
          it cannot cover the thing somebody is waiting to see. */}
      <Busy on={busy || work.on} done={work.done} total={work.total} label={work.label} />

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
            svgRef={svgRef}
          />
        ) : (
          <div className="canvas empty">
            <div>
              <h2 className="section">{unopened ? "Nothing here yet" : "Nothing open"}</h2>
              <p className="lede">
                {unopened ? (
                  <>
                    Nothing has been fetched for <span className="mono">{short(unopened)}</span>.
                    Reading never reaches the network — this is the one button that
                    does, and it fetches that address&rsquo;s transfers on{" "}
                    {CHAINS.find(([id]) => id === chain)?.[1] ?? `chain ${chain}`}.
                  </>
                ) : (
                  <>
                    Type an address to open it. Reading never reaches the network;
                    only <em>follow the money from here</em> does, and only when you
                    press it.
                  </>
                )}
              </p>
              {/* An empty case had no first move: `open` reads the store, and the
                  only control that fetches lives on a selected node, which an
                  empty case has none of. Starting from nothing --- how everybody
                  starts --- dead-ended on "Run an analysis first", naming a step
                  that does not exist. */}
              {unopened ? (
                <button className="primary" onClick={() => void seedFetch(unopened)} disabled={busy}>
                  {busy ? <Spinner /> : null}
                  {busy ? "fetching" : `fetch ${short(unopened)}`}
                </button>
              ) : null}
            </div>
          </div>
        )}

        <Selected
          address={selected}
          chain={`eip155:${chain}`}
          frontier={(graph?.nodes ?? []).filter((n) => n.frontier).map((n) => n.address)}
          highlight={pending}
          onWork={setWork}
          onReload={() => load(address)}
          say={say}
        />
      </main>

      {timeRange ? (
        <div className="timebar">
          <span className="muted">up to</span>
          <input
            type="range"
            aria-label="show flows up to this time"
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

      {/* A live region: the status line is where truncation and fetch results
          are reported, and a reader who cannot see it would otherwise never
          learn that a walk stopped early. */}
      <footer
        className={`case-status ${status.tone}`}
        role="status"
        aria-live="polite"
      >
        <span>{status.text}</span>
        {/* Beside the counts, not below them: "29 flows" and "3 reads failed"
            are one sentence, and separating them lets the first be read
            without the second. */}
        <Activity busy={busy || work.on} />
      </footer>

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
