"use client";

/**
 * What the server read, and what it failed to read.
 *
 * The status bar reports the outcome — "22 addresses · 29 flows" — and that
 * sentence is identical whether the walk saw everything or a provider refused
 * a third of it. This is the other half: one row per read, with the failures
 * counted separately and coloured, because a picture built on three failed
 * pages is short by whatever those pages carried and nothing else on screen
 * says so.
 *
 * Collapsed to a single count by default. An investigator does not want a
 * scrolling log across the bottom of a case; they want to know the number is
 * zero, and to open it when it is not.
 *
 * Polled only while a fetch is running, and once when it stops. There is
 * nothing to watch when nothing is being read, and a timer that keeps firing
 * against a local server is a thing that shows up in somebody's battery.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";

type Event = {
  at: number;
  provider: string;
  chain: string;
  what: string;
  address: string;
  outcome: "ok" | "empty" | "more" | "capped" | "failed";
  rows: number;
  ms: number;
  detail: string;
};

type Reply = {
  events: Event[];
  counts: {
    ok: number;
    empty: number;
    more: number;
    capped: number;
    failed: number;
    total: number;
  };
};

/** What each outcome means, in the place somebody reads it. */
const MEANS: Record<Event["outcome"], string> = {
  ok: "rows came back",
  empty: "the provider answered, and the answer was nothing",
  more: "a full page — there is more beyond it",
  capped:
    "the provider will not page further. What was fetched is real; there is " +
    "more it cannot reach, so the answer is a prefix.",
  failed: "no answer. What was drawn is missing these rows.",
};

export function Activity({ busy }: { busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [reply, setReply] = useState<Reply | null>(null);

  const poll = useCallback(async () => {
    try {
      setReply(await api<Reply>("/activity", { limit: "60" }));
    } catch {
      // The log is a diagnostic. Failing to read it must not put an error in
      // front of somebody who is already looking at one.
    }
  }, []);

  useEffect(() => {
    void poll();
    if (!busy && !open) return;
    const timer = setInterval(poll, busy ? 900 : 4000);
    return () => clearInterval(timer);
  }, [busy, open, poll]);

  const counts = reply?.counts;
  if (!counts?.total) return null;

  return (
    <div className={open ? "activity open" : "activity"}>
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="mono">{counts.total} reads</span>
        {counts.failed ? <span className="mono bad">{counts.failed} failed</span> : null}
        {counts.capped ? (
          <span className="mono capped">{counts.capped} capped</span>
        ) : null}
        {!counts.failed && !counts.capped ? (
          <span className="mono quiet">none failed</span>
        ) : null}
      </button>

      {open ? (
        <div className="rows">
          {counts.failed ? (
            <p className="cannot">
              <b>{counts.failed} read(s) got no answer.</b> Everything drawn from
              them is missing those rows — which is not the same as the money
              stopping there.
            </p>
          ) : null}
          {/* Separated from a failure on purpose. A ceiling is the endpoint
              working as documented, and the move it calls for is a narrower
              window or another provider, not a retry. */}
          {counts.capped ? (
            <p className="cannot">
              <b>{counts.capped} read(s) hit a provider ceiling.</b> What was
              fetched is real; the provider will not serve past that point, so
              this case is a prefix. Narrow the window or use a provider with a
              deeper history.
            </p>
          ) : null}
          <table>
            <thead>
              <tr>
                <th>when</th>
                <th>provider</th>
                <th>read</th>
                <th>address</th>
                <th className="num">rows</th>
                <th className="num">ms</th>
                <th>outcome</th>
              </tr>
            </thead>
            <tbody>
              {(reply?.events ?? []).map((event, i) => (
                <tr key={`${event.at}-${i}`} className={event.outcome}>
                  <td className="mono quiet">
                    {new Date(event.at * 1000).toISOString().slice(11, 19)}
                  </td>
                  <td className="mono">{event.provider}</td>
                  <td className="mono quiet">{event.what}</td>
                  <td className="mono quiet">{short(event.address)}</td>
                  <td className="mono num">{event.rows}</td>
                  <td className="mono num quiet">{event.ms}</td>
                  <td className="mono" title={event.detail || MEANS[event.outcome]}>
                    {event.outcome}
                    {event.detail ? <span className="quiet"> — {event.detail}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function short(address: string): string {
  return address.length > 14 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}
