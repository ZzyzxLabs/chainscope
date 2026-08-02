# What investigators need, and how we know

A requirements note with a rule: **every item says where the evidence came
from.** Anything marked *observed* was hit while doing the work --- a challenge
write-up recorded it, or it broke while this tool was being used. Anything
marked *inferred* is reasoning from the observed items and should be treated as
a hypothesis until somebody hits it.

The distinction matters because a roadmap built from imagined users produces
features nobody asked for, and this project's whole argument is about not
presenting guesses as findings.

---

## 1. The failure that costs the most is silence

**Observed.** Three separate write-ups record the same shape, and each one cost
a wrong answer rather than an error:

- An archive endpoint's `eth_getLogs` returned a log when asked for one block
  and dropped it in 500-block ranges. HTTP 200, no error. One withdrawal
  address of thirteen went missing.
- A block number off by one hex digit returned a *different* block. Timestamps
  from it put an event days away, across four chains, and the submitted answer
  was wrong.
- A cache stored a rate-limit response as data. Re-running did not help,
  because re-running made no request.

The common structure: the tool had an answer, the answer looked normal, and
nothing anywhere said "this might be wrong".

**What follows.** Every enumerative result needs a completeness statement, not
a footnote. Shipped: `Router.corroborate` (two independent sources, reports
what only one saw), block identity verification, `ResultTruncated` on every
enumerating provider, `is_cacheable` predicates.

**Still open.** Corroboration is opt-in per call site. An investigator who does
not know it exists gets a single-source answer that does not say so. *Inferred:*
the default for enumeration should be corroborated, with single-source as the
explicit choice.

---

## 2. A technique nobody can reach does not exist

**Observed.** Three times in one session a technique was implemented, measured
against ground truth, and left invisible: usable from Python, absent from the
CLI and the agent. Twice more, an analyzer was *registered* but broken --- its
entry point pointed at a bare function, and the failure surfaced as "needs
constructor arguments (a data source)", which is true of something else.

**What follows.** Registration is a promise about a type, and a promise nothing
checks is decoration. Shipped: the entry-point contract test reads installed
metadata, so a tenth analyzer registered wrongly fails without anyone
remembering to check. The skill-currency test does the same for documentation
--- it caught five capabilities the skill did not mention, and a `dashboard`
section that had never existed.

**Still open.** *Inferred:* the same check should extend to the browser
extension and the MCP tool list. Nothing currently fails when a capability
exists and no surface exposes it.

---

## 3. Units are where confident wrongness lives

**Observed.**

- BSC and Polygon native transfers came back denominated in ETH. Right number,
  wrong unit, and the number reads fine.
- The store's transfer identity omitted `asset`, so two transfers of equal raw
  amounts of different tokens collided and one was silently dropped. Measured:
  two rows in, one row out.
- The graph ranked counterparties by raw amount across assets, so 18-decimal
  dust outranked 5,000 USDC and consumed the budget.
- A dashboard assumed 18 decimals, rendering a six-decimal balance a trillion
  times too small --- small enough to read as dust and be skipped.

**What follows.** Raw amounts compare within an asset and nowhere else. Shipped:
per-chain native symbols, `asset` in the uniqueness key, per-asset ranking and
edge widths, decimals carried end to end.

**Still open.** Cross-asset comparison needs prices, and prices need a source
with its own provenance and staleness. *Inferred:* this is the next real
capability, and it should refuse rather than interpolate when no rate exists for
a timestamp --- the cross-chain matcher already ranks a decoy first when the
true payout is absent, and a price source that guesses would make that worse.

---

## 4. Provenance has to be unforgeable, not merely required

**Observed.** The loopback server let a request choose its own `source`. Any
page in the user's browser can reach it, so a claim could be written labelled
"OFAC SDN list" and would sit in the store indistinguishable from a real import
of one. `Attribution` refuses to be constructed without a source precisely so
every claim can be traced back; letting the claim pick what it says defeats the
type it is stored in.

**What follows.** Shipped: the origin marker is the server's and cannot be
replaced; caller text is appended and marked *reported*.

**Still open.** *Inferred:* multi-person cases need per-analyst identity, not
just per-agent. Two people tagging into one store currently differ only if they
configured different agent names. Signed attributions would make a shared case
file auditable; that is a real design question and not a small one.

---

## 5. What "professional visualization" actually means

**Observed.** A force-directed graph answers "who is connected to whom". The
question an investigation asks is "where did the money go", and a spring layout
renders a five-hop laundering chain and a five-way split identically.

**What follows.** Shipped: the layered flow view, columns by hop distance,
per-asset edge widths, frontier drawn as frontier, truncation on the canvas.

The three things MetaSleuth and MistTrack had that this did not --- a scrubable
time axis, path highlighting, and click-to-expand --- are **all three shipped**,
in the *inferred* priority order below. The order came from what the challenge
work actually needed rather than from feature parity, which is why the one
those products lead with came last here:

1. **Path highlight** --- the CH08 case is a tree of routes and reading one
   route at a time is the whole task. Every route from a seed, not the
   shortest: a split that rejoins is the structure worth seeing.
2. **Click to expand** --- folded hops ship inside the page, since a file://
   document cannot fetch. One ring per click, so there is always either a `+n`
   or a frontier marker and never a picture that merely stops.
3. **Time scrubbing** --- last, because `temporal` already answers timing
   questions in text. An edge is an aggregate over a span, so "active by T"
   means it *started* by T, and an undated edge is shown at every position:
   a provider omitting a timestamp is not evidence about when money moved.

The route finder, the reveal rule, and the scrub predicate all execute under
Node in the test suite. They are the substantive half of this view and they
live in JavaScript, where the Python suite cannot reach --- shipping them
unexercised would repeat what this project keeps finding elsewhere.

**Still open.** Click-to-expand reveals what was walked and cannot walk
further; going past the outermost ring needs a live fetch, which needs the
loopback server and a token. *Inferred:* worth doing, and it trades away the
property that the file works with no network.

---

## 6. Needs nobody has hit yet

Everything here is *inferred*. Listed so they are not mistaken for observed,
and so the list is not silently empty.

- **Case handoff.** Bundles exist; whether they survive a colleague opening one
  on a different machine with different providers is untested.
- **Label conflict at scale.** Conflicts are reported per import. A store with
  several sources disagreeing about hundreds of addresses has no triage view.
- **Provider budgets.** Rate limits are per-client. Nothing tracks spend against
  a paid quota, and running out mid-trace is a partial answer.
- **Non-EVM breadth.** Sui, Bitcoin, Solana and Tron are modelled; only EVM has
  two independent sources, so corroboration cannot run elsewhere.
- ~~**Backwards taint.**~~ Shipped: `trace_origins` and the agent's
  `trace_origins_of`. This was the justification for choosing FIFO over
  haircut, so leaving it unbuilt meant the choice was argued and not used.
  Haircut cannot do it at all: proportional splitting mixes every source into
  every output, so a balance can be called 3% tainted and never *which* 3%.

---

## What this note is not

It is not a survey of users, because none has been conducted. Every *observed*
item comes from one team's investigations and from using the tool while building
it. That is a narrow sample with a real bias: it over-weights what breaks during
a CTF-shaped investigation with a known answer, and under-weights the long,
open-ended kind where nobody knows whether there is anything to find.

The honest version of "I thought about their needs" is this: here is what we
watched go wrong, here is what we inferred from it, and here is the line between
the two.
