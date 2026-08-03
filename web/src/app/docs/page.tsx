/**
 * The documentation page.
 *
 * Long-form, one column, with a sticky table of contents — the shape that reads
 * best for reference material somebody returns to. It shares its source of
 * truth with the landing page (`lib/views.ts`), so a view cannot be described
 * two ways.
 */

import type { Metadata } from "next";
import Link from "next/link";

import { CONTRACT, ENDPOINTS, VIEWS } from "@/lib/views";

export const metadata: Metadata = {
  title: "chainscope — documentation",
  description:
    "Every view, every endpoint, and what each one cannot tell you.",
};

const SECTIONS = [
  ["the-rule", "The rule"],
  ["views", "The views"],
  ["reading", "Reading a result"],
  ["endpoints", "HTTP endpoints"],
  ["agents", "For agents"],
  ["privacy", "Privacy"],
] as const;

export default function Docs() {
  return (
    <main className="band frame">
      <div className="docs">
        <nav className="toc" aria-label="contents">
          {SECTIONS.map(([id, label]) => (
            <a key={id} href={`#${id}`}>
              {label}
            </a>
          ))}
        </nav>

        <article className="prose">
          <h2 id="the-rule">The rule everything here follows</h2>
          <p>
            An absence must never be indistinguishable from a result. A source
            that could not be read, a walk stopped by a node limit, a filter
            that matched nothing, and a time window that missed the transfer all
            produce an empty answer — and each is reported as itself, because
            only one of them means there is nothing there.
          </p>
          <p>
            This is the reason for most of the design decisions below: why
            frontier nodes are drawn differently from leaves, why{" "}
            <code>filtered_out</code> is a field, why a hypothesis cannot claim
            more than MEDIUM confidence, and why a bundle without its query
            cache says so instead of looking complete.
          </p>

          <h2 id="views">The views</h2>
          {VIEWS.map((view, i) => (
            <section key={view.name}>
              <h3>
                <span className="mono muted">
                  {String(i + 1).padStart(2, "0")}
                </span>{" "}
                {view.name}
              </h3>
              <p className="cell-where">{view.where}</p>
              <p>{view.what}</p>
              <p className="for-this">
                <b>Use it to</b> {view.use}
              </p>
              <p className="cannot">
                <b>It cannot tell you</b> {view.cannot}
              </p>
            </section>
          ))}

          <h2 id="reading">Reading a result</h2>
          <p>
            Six fields decide whether an answer means what it appears to. Check
            them before reporting that nothing was found.
          </p>
          {CONTRACT.map((item) => (
            <section key={item.term}>
              <h3 className="mono" style={{ fontSize: 14 }}>
                {item.term}
              </h3>
              <p>{item.meaning}</p>
            </section>
          ))}

          <h2 id="endpoints">HTTP endpoints</h2>
          <p>
            Loopback only, token-authenticated, same-origin. Every response that
            could be partial says so in a field rather than by being shorter.
          </p>
          {ENDPOINTS.map((endpoint) => (
            <section key={endpoint.http}>
              <p className="endpoint">{endpoint.http}</p>
              <p>
                <b>{endpoint.name}</b> — {endpoint.description}
              </p>
            </section>
          ))}

          <h2 id="agents">For agents</h2>
          <p>
            An MCP server exposes this same surface as tools (
            <code>chainscope-mcp</code>). Machine-readable discovery lives at{" "}
            <a href="/llms.txt">/llms.txt</a> and{" "}
            <a href="/.well-known/agent.json">/.well-known/agent.json</a>.
          </p>
          <p>
            Three properties are built in rather than left to prompting, because
            an agent is a confident narrator and a forensics tool whose output
            can be paraphrased into certainty is worse than no tool:
          </p>
          <ul>
            <li>
              <b>Every claim carries its provenance as fields on the same
              object</b> — the label, source, confidence and rationale arrive
              together, so omitting them is a visible choice rather than an
              accident of formatting.
            </li>
            <li>
              <b>Writing is opt-in and self-identifying.</b> It is off by
              default, and a label written by an agent records which one. Six
              months later a reviewer has to be able to tell a model&rsquo;s
              suggestion from a person&rsquo;s judgement, and that cannot be
              recovered after the fact.
            </li>
            <li>
              <b>Amounts are strings.</b> They are wei-scale, JSON numbers are
              IEEE 754 doubles, and 10 ETH already exceeds what one holds
              exactly. A silently rounded balance survives into a report.
            </li>
          </ul>

          <h2 id="privacy">Privacy</h2>
          <p>
            No CDN, no runtime font fetches, no telemetry, no map tiles. A
            forensics tool that phones a third party on load tells that third
            party which addresses are being investigated. The server binds to{" "}
            <code>127.0.0.1</code>; the case view inlines everything it needs;
            these pages self-host their fonts at build time.
          </p>

          <p style={{ marginTop: 40 }}>
            <Link className="cta ghost" href="/">
              back
            </Link>
          </p>
        </article>
      </div>
    </main>
  );
}
