/**
 * The landing page.
 *
 * It argues one thing, because the tool is built around one thing: an absence
 * must never be indistinguishable from a result. Everything else — the graph,
 * the analyzers, the bundle format — follows from that, so the page leads with
 * it rather than with a feature list.
 *
 * Every view is listed with what it cannot tell you, in a differently-coloured
 * block. That is not hedging. A reader who takes a truncated graph for a
 * complete one will act on it, and the only moment they can be told is before
 * they open it.
 */

import Link from "next/link";

import { ANALYSES, CONTRACT, VIEWS } from "@/lib/views";

export default function Home() {
  return (
    <main>
      <section className="band frame">
        <p className="eyebrow">open-source blockchain forensics</p>
        <h1 className="display">
          A case you can
          <br />
          hand to somebody else.
        </h1>
        <p className="lede">
          Every claim carries where it came from and how sure it is. Every
          absence says whether it is an absence of evidence or an absence of
          looking. Heuristic output is a lead, not evidence — and the tool says
          which, at every point, rather than leaving you to remember.
        </p>
        <div className="cta-row">
          <Link className="cta" href="/case/">
            open a case
          </Link>
          <Link className="cta ghost" href="/docs">
            read the docs
          </Link>
        </div>
      </section>

      <section className="band frame">
        <div className="callout stark">
          <p className="eyebrow" style={{ opacity: 0.7 }}>
            the rule everything here follows
          </p>
          <p className="lede" style={{ marginBottom: 0 }}>
            A source that could not be read, a walk stopped by a node limit, a
            filter that matched nothing, and a window that missed the transfer
            all produce an empty answer. Each is reported as itself — because
            only one of them means there is nothing there.
          </p>
        </div>
      </section>

      <section className="band frame" id="views">
        <h2 className="section">What it shows you</h2>
        <p className="lede">
          {/* Counted, not written down. A ninth view would otherwise leave the
              prose saying eight, and the number is the first thing read. */}
          {VIEWS.length} views. Each one listed with what it is for, and with
          what an answer from it does not settle.
        </p>
        <div className="grid">
          {VIEWS.map((view, i) => (
            <article className="cell" key={view.name}>
              <p className="cell-n">{String(i + 1).padStart(2, "0")}</p>
              <h3 className="item">{view.name}</h3>
              <p className="cell-where">{view.where}</p>
              <p>{view.what}</p>
              <p className="for-this">
                <b>Use it to</b> {view.use}
              </p>
              <p className="cannot">
                <b>It cannot tell you</b> {view.cannot}
              </p>
            </article>
          ))}
        </div>
      </section>

      <section className="band frame" id="analyses">
        <h2 className="section">The analyses</h2>
        <p className="lede">
          Thirteen, each answering one question. Open one against an address and
          it runs in the case view — findings and hypotheses reported separately,
          because one is observed and the other inferred.
        </p>
        <div className="grid">
          {ANALYSES.map((item, i) => (
            <article className="cell" key={item.name}>
              <p className="cell-n">{String(i + 1).padStart(2, "0")}</p>
              <h3 className="item mono" style={{ fontSize: 14 }}>
                {item.name}
              </h3>
              <p>{item.what}</p>
              <p className="cannot">
                <b>It cannot tell you</b> {item.cannot}
              </p>
              <p style={{ marginTop: 12 }}>
                <Link className="cta ghost" href={`/case/?run=${item.name}`}>
                  run it
                </Link>
              </p>
            </article>
          ))}
        </div>
      </section>

      <section className="band frame">
        <h2 className="section">Reading a result</h2>
        <p className="lede">
          Six fields decide whether an answer means what it appears to. They are
          on the responses, in the agent card, and here — because a person
          reading docs and a model reading <code>/llms.txt</code> make the same
          mistake: reporting an empty result as a finding.
        </p>
        <div className="grid">
          {CONTRACT.map((item, i) => (
            <article className="cell" key={item.term}>
              <p className="cell-n">{String(i + 1).padStart(2, "0")}</p>
              <h3 className="item mono" style={{ fontSize: 14 }}>
                {item.term}
              </h3>
              <p>{item.meaning}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="band frame">
        <h2 className="section">It runs on your machine</h2>
        <p className="lede">
          Loopback only. No CDN, no fonts fetched at runtime, no telemetry, no
          map tiles. A forensics tool that phones a third party on load tells
          that third party which addresses are being investigated, which is the
          one thing it must never do.
        </p>
        <div className="cta-row">
          <Link className="cta" href="/docs">
            documentation
          </Link>
          <a className="cta ghost" href="/llms.txt">
            llms.txt
          </a>
          <a className="cta ghost" href="/.well-known/agent.json">
            agent card
          </a>
        </div>
      </section>

      <footer className="foot">
        <div className="frame">
          chainscope — heuristic output is a lead, not evidence.{" "}
          <Link href="/docs">docs</Link>
        </div>
      </footer>
    </main>
  );
}
