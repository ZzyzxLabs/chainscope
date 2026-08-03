"""Case dashboard: what a store contains, and what is unfinished about it.

The graph view answers "where did the money go". This answers the questions
that come before and after it --- how much of this case is actually known, what
is still unexamined, which claims are weak, and what has fired.

**The design decision worth stating: coverage is the headline, not volume.** A
dashboard that opens with "1,284 transfers, 96 addresses" tells an investigator
they have been busy. The numbers that decide whether a conclusion is defensible
are the other ones: how many addresses carry no attribution, how many were seen
but never expanded, how many claims rest on inference rather than evidence.
Those are what a reviewer will ask about, and a summary that buries them is
optimised for the wrong reader.

So the top row is unlabelled, frontier, and low-confidence counts, and each is
phrased as work outstanding rather than as a statistic.

Self-contained like the graph view, and for the same reason: an exhibit that
fetches anything at load time stops working eventually, and it stops working
precisely when somebody needs it.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from .html import _json_for_script

__all__ = ["CaseSummary", "to_dashboard"]


@dataclass
class CaseSummary:
    """Everything the dashboard renders. Assembled by the caller from a store.

    A plain data object rather than something that queries: the same summary
    should be producible from a live store, a case bundle, or a fixture, and
    the renderer should not know which it got.
    """

    title: str = "case"
    store_path: str = ""
    chains: list[str] = field(default_factory=list)

    transfers: int = 0
    addresses: int = 0
    attributions: int = 0

    unlabelled: int = 0
    frontier: int = 0
    low_confidence: int = 0
    """Claims at LOW or SPECULATIVE. Not a defect --- an honest investigation
    has plenty --- but a reviewer will ask, and burying the count invites a
    reader to treat every label as equally solid."""

    totals_by_asset: list[tuple[str, str, int, int | None]] = field(default_factory=list)
    """``(symbol, raw_total_as_string, transfer_count, decimals)``.

    Strings because these exceed what a JSON number holds exactly, and never
    summed across assets --- that produces a figure denominated in nothing.

    ``decimals`` is carried rather than assumed. Without it the renderer used
    18 for everything, and 1,000 USDC --- six decimals --- was displayed as
    ``0.000000``. ``None`` means the store does not know, and the raw integer
    is then shown as a raw integer instead of being scaled by a guess."""

    top_flows: list[dict[str, Any]] = field(default_factory=list)
    categories: list[tuple[str, int]] = field(default_factory=list)
    sources: list[tuple[str, int]] = field(default_factory=list)
    """Where the labels came from, by count. A case whose attribution rests
    entirely on one dump is a case with one point of failure."""

    events: list[dict[str, Any]] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    @property
    def coverage(self) -> float:
        """Share of addresses carrying any attribution, 0--1."""
        return 0.0 if not self.addresses else 1 - (self.unlabelled / self.addresses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "chains": self.chains,
            "transfers": self.transfers,
            "addresses": self.addresses,
            "attributions": self.attributions,
            "unlabelled": self.unlabelled,
            "frontier": self.frontier,
            "low_confidence": self.low_confidence,
            "coverage": round(self.coverage, 3),
            "totals_by_asset": [
                {"symbol": s, "total_raw": t, "transfers": n, "decimals": d}
                for s, t, n, d in self.totals_by_asset
            ],
            "top_flows": self.top_flows,
            "categories": [{"name": c, "count": n} for c, n in self.categories],
            "sources": [{"name": s, "count": n} for s, n in self.sources],
            "events": self.events,
        }


_CSS = """
:root { --bg:#fbfbfa; --fg:#16161a; --muted:#6b7280; --line:#e4e4e7;
        --panel:#fff; --warn:#b45309; --bad:#c62828; --ok:#2e7d32; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#131316; --fg:#e8e8ea; --muted:#9b9ba3; --line:#2e2e35;
          --panel:#1b1b20; --warn:#d97706; --bad:#ef5350; --ok:#66bb6a; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); padding:0 0 48px;
  font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }
header { padding:22px 28px 16px; border-bottom:1px solid var(--line); }
h1 { margin:0 0 4px; font-size:17px; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:12px; }
main { max-width:1100px; margin:0 auto; padding:0 28px; }
section { margin-top:28px; }
h2 { font-size:11px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); margin:0 0 10px; font-weight:600; }
.cards { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; }
.card .n { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; line-height:1.2; }
.card .k { color:var(--muted); font-size:12px; margin-top:2px; }
.card .why { color:var(--muted); font-size:11px; margin-top:8px;
  border-top:1px solid var(--line); padding-top:8px; }
.card.warn .n { color:var(--warn); }
.card.bad .n { color:var(--bad); }
table { width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }
th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums; }
th { color:var(--muted); font-size:11px; text-transform:uppercase;
  letter-spacing:.05em; font-weight:600; }
tr:last-child td { border-bottom:none; }
td.n { text-align:right; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:8px; }
.bar > i { display:block; height:100%; background:var(--ok); }
.pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
  border:1px solid var(--line); color:var(--muted); }
.pill.urgent { color:var(--bad); border-color:var(--bad); }
.note { color:var(--muted); font-size:12px; margin-top:10px; }
.scroll { overflow-x:auto; }
.empty { color:var(--muted); padding:14px; background:var(--panel);
  border:1px dashed var(--line); border-radius:8px; font-size:13px; }
"""


#: Fraction digits kept once something is actually visible.
_PLACES = 6


def _fmt(raw: str, decimals: int = 18) -> str:
    """Render an exact integer amount without ever touching a float.

    Six *significant* fraction digits, not six fraction digits. Cutting at a
    fixed position turned one wei into ``0.000000`` and 0.000012345 ETH into
    ``0.000012`` --- the first reads as nothing moved, which is the wrong thing
    to tell a reader looking at a peel chain or an address-poisoning transfer,
    where a dust amount *is* the signal. So when the whole part is zero the
    leading zeros of the fraction are counted separately from the digits kept,
    and a small number stays small rather than becoming none.
    """
    negative = raw.startswith("-")
    digits = (raw[1:] if negative else raw).rjust(decimals + 1, "0")
    whole = digits[: len(digits) - decimals] or "0"
    frac = digits[len(digits) - decimals :].rstrip("0") if decimals else ""
    if frac:
        keep = _PLACES
        if int(whole) == 0:
            keep += len(frac) - len(frac.lstrip("0"))
        frac = frac[:keep]
    grouped = f"{int(whole):,}"
    return ("-" if negative else "") + grouped + (f".{frac}" if frac else "")


def _card(value: str, label: str, why: str = "", tone: str = "") -> str:
    klass = f"card {tone}".strip()
    tail = f'<div class="why">{html.escape(why)}</div>' if why else ""
    return (
        f'<div class="{klass}"><div class="n">{html.escape(value)}</div>'
        f'<div class="k">{html.escape(label)}</div>{tail}</div>'
    )


def _cell(value: object, *, klass: str = "", mono: bool = False) -> str:
    classes = " ".join(c for c in (klass, "mono" if mono else "") if c)
    attr = f' class="{classes}"' if classes else ""
    return f"<td{attr}>{html.escape(str(value))}</td>"


def _flow_row(flow: dict[str, Any]) -> str:
    """One row of the largest-flows table.

    A function rather than an inline f-string: the amount needs its own
    decimals to render, and nesting that lookup inside a comprehension made the
    quoting unreadable enough to hide a mistake.
    """
    sender = str(flow.get("sender", ""))[:18]
    recipient = str(flow.get("recipient", ""))[:18]
    decimals = int(flow.get("decimals", 18))
    amount = _fmt(str(flow.get("total_raw", "0")), decimals)
    return (
        "<tr>"
        + _cell(f"{sender}…", mono=True)
        + _cell(f"{recipient}…", mono=True)
        + _cell(amount, klass="n", mono=True)
        + _cell(flow.get("symbol", ""))
        + _cell(f"{int(flow.get('transfers', 0)):,}", klass="n")
        + "</tr>"
    )


def _event_row(event: dict[str, Any]) -> str:
    severity = str(event.get("severity", ""))
    return (
        "<tr><td>"
        f'<span class="pill {html.escape(severity)}">{html.escape(severity)}</span>'
        "</td>"
        + _cell(event.get("watch", ""))
        + _cell(str(event.get("reason", ""))[:110])
        + _cell(f"{str(event.get('tx', ''))[:14]}…", mono=True)
        + "</tr>"
    )


def to_dashboard(summary: CaseSummary) -> str:
    """Render a case summary as one self-contained HTML document."""
    s = summary
    # Computed before the template, not inside it: a backslash in an f-string
    # expression is a syntax error before Python 3.12, and this package
    # supports 3.10. Escaped through the same helper the graph view uses ---
    # json.dumps alone is not safe inside a <script> element, because the HTML
    # parser looks for "</script" without caring that it sits inside a string.
    payload = _json_for_script(s.to_dict())

    # Coverage first, and phrased as work outstanding. "96 addresses" says an
    # investigator has been busy; "41 unlabelled" says what a reviewer will ask.
    outstanding = "".join(
        [
            _card(
                f"{s.unlabelled:,}",
                "addresses with no attribution",
                "Absence of a label is not evidence of anything --- only that "
                "nobody has looked, or that no source covers it.",
                "warn" if s.unlabelled else "",
            ),
            _card(
                f"{s.frontier:,}",
                "seen but never expanded",
                "The case stops here because nobody followed further, not "
                "because there is nothing further.",
                "warn" if s.frontier else "",
            ),
            _card(
                f"{s.low_confidence:,}",
                "claims at low confidence or below",
                "Not a defect --- an honest investigation has plenty. Each "
                "carries a rationale; a reviewer will read them.",
            ),
            _card(f"{s.coverage:.0%}", "attribution coverage"),
        ]
    )

    volume = "".join(
        [
            _card(f"{s.transfers:,}", "transfers"),
            _card(f"{s.addresses:,}", "addresses"),
            _card(f"{s.attributions:,}", "attribution claims"),
            _card(str(len(s.chains)) if s.chains else "0", "chains"),
        ]
    )

    totals = (
        "".join(
            "<tr>"
            + _cell(sym or "native")
            + _cell(
                _fmt(total, places) if places is not None else f"{int(total):,} raw",
                klass="n",
                mono=True,
            )
            + _cell(f"{count:,}", klass="n")
            + "</tr>"
            for sym, total, count, places in s.totals_by_asset
        )
        or '<tr><td colspan="3">nothing recorded</td></tr>'
    )

    flows = "".join(_flow_row(f) for f in s.top_flows) or (
        '<tr><td colspan="5">no flows recorded</td></tr>'
    )

    def _rows(pairs: list[tuple[str, int]], empty: str) -> str:
        return (
            "".join(
                "<tr>" + _cell(name) + _cell(f"{count:,}", klass="n") + "</tr>"
                for name, count in pairs
            )
            or f'<tr><td colspan="2">{html.escape(empty)}</td></tr>'
        )

    events = (
        "".join(
            f'<tr><td><span class="pill {html.escape(str(e.get("severity", "")))}">'
            f"{html.escape(str(e.get('severity', '')))}</span></td>"
            f"<td>{html.escape(str(e.get('watch', '')))}</td>"
            f"<td>{html.escape(str(e.get('reason', ''))[:110])}</td>"
            f'<td class="mono">{html.escape(str(e.get("tx", ""))[:14])}…</td></tr>'
            for e in s.events
        )
        or '<tr><td colspan="4">no watches evaluated over this store</td></tr>'
    )

    single_source = ""
    if len(s.sources) == 1 and s.attributions:
        single_source = (
            '<p class="note">Every claim in this case comes from one source. '
            "That is one point of failure for the entire attribution layer.</p>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(s.title)}</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>{html.escape(s.title)}</h1>
  <div class="sub">
    {html.escape(", ".join(s.chains) or "no chains")}
    {
        " &middot; " + html.escape(s.first_seen[:10]) + " to " + html.escape(s.last_seen[:10])
        if s.first_seen
        else ""
    }
    {" &middot; " + html.escape(s.store_path) if s.store_path else ""}
  </div>
</header>
<main>
  <section>
    <h2>What is unfinished</h2>
    <div class="cards">{outstanding}</div>
    <div class="bar"><i style="width:{s.coverage:.0%}"></i></div>
    <p class="note">Coverage leads because it decides whether a conclusion is
    defensible. Volume does not.</p>
  </section>

  <section>
    <h2>Volume</h2>
    <div class="cards">{volume}</div>
  </section>

  <section>
    <h2>Totals by asset</h2>
    <div class="scroll"><table>
      <tr><th>asset</th><th class="n">total</th><th class="n">transfers</th></tr>
      {totals}
    </table></div>
    <p class="note">Exact integers, never combined across assets &mdash; a sum
    over two denominations is a figure denominated in nothing.</p>
  </section>

  <section>
    <h2>Largest flows</h2>
    <div class="scroll"><table>
      <tr><th>from</th><th>to</th><th class="n">total</th><th>asset</th>
          <th class="n">transfers</th></tr>
      {flows}
    </table></div>
  </section>

  <section>
    <h2>Attribution</h2>
    <div class="cards" style="grid-template-columns:1fr 1fr">
      <div><table>
        <tr><th>category</th><th class="n">addresses</th></tr>
        {_rows(s.categories, "no categories recorded")}
      </table></div>
      <div><table>
        <tr><th>source</th><th class="n">claims</th></tr>
        {_rows(s.sources, "no sources recorded")}
      </table></div>
    </div>
    {single_source}
  </section>

  <section>
    <h2>Watch events</h2>
    <div class="scroll"><table>
      <tr><th>severity</th><th>watch</th><th>reason</th><th>tx</th></tr>
      {events}
    </table></div>
  </section>
</main>
<script type="application/json" id="data">{payload}</script>
</body></html>"""
