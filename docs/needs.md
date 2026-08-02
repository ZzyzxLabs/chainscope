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

**And it was not enough.** *Observed:* the emitted page had **never parsed**. A
`\"` inside the Python template collapsed to a bare quote in the JavaScript, so
the whole `<script>` was a syntax error and nothing in it ran --- no graph, no
click-to-expand, no scrub. The file opened, drew a header, and showed an empty
canvas. Every JavaScript test passed throughout, because they extract
individual functions with a regex and run those: **a fragment that parses says
nothing about the file.** It was found by opening the page in a browser, which
no test did.

The fix is one test that renders a real graph, takes the script tag a browser
would take, and asks `node --check` whether it is a program. This is the same
lesson as §2 in a new place: a check that exercises a *part* of an artefact is
not a check on the artefact, and the gap it leaves is invisible from inside the
suite.

**Still open.** Click-to-expand reveals what was walked and cannot walk
further; going past the outermost ring needs a live fetch, which needs the
loopback server and a token. *Inferred:* worth doing, and it trades away the
property that the file works with no network.

### What an hour inside MetaSleuth showed

**Observed** --- driving the product, signed in, on the Ronin bridge exploiter.
The gap is not any single feature. It is that **their graph is a document and
ours is an export.**

Theirs opens as `untitled ✎` with a save button, undo and redo, and a share
link. Everything on the canvas is editable: rename a node, recolour it, delete
it, attach a rich-text memo to it, drop an arbitrary address on with *Add
Address / Tx*, or draw an edge between two nodes by hand. The address filter is
a searchable checkbox roster of every node currently on the canvas --- which
doubles as the answer to "what is in this picture", a question our view cannot
be asked. Sharing takes a snapshot and hands over a link; the recipient edits
their own copy, and a toggle decides whether your private labels travel with it.

Two of those are worth taking as they are, and one is worth refusing:

- **The roster.** *Shipped.* A searchable list of every node with a visibility
  checkbox, and the header states the count out loud --- "7 of 7, 1 hidden by
  you". That sentence is the point: a picture quietly missing what somebody hid
  is a picture that looks complete and is not.
- **Persistence.** *Shipped.* Hidden nodes, opened folds, dragged offsets, the
  chosen asset and per-node names round-trip --- to `localStorage` for a
  refresh, and to a `.canvas.json` file for anything that leaves the machine.

  The design decision worth naming: **state is keyed by address, never by
  position in the arrays.** Re-run `chainscope graph` at a greater depth, load
  the saved canvas into the new page, and the work survives. Keyed by index it
  would silently reattach somebody's note to a different address, which is
  worse than losing it. State for an address the current view does not contain
  is **kept and counted**, not dropped --- a narrower depth is a different
  question about the same case.

  Renaming is allowed and is marked as yours every time it is shown, with the
  panel saying it is not an attribution and does not travel outside the canvas.
  A name somebody typed and a sourced claim must not read as the same
  statement; that is what the type system enforces everywhere else, applied to
  the picture.
- **The hand-drawn edge, as they have it, is not worth copying.** An edge
  somebody drew is a hypothesis; an edge from a transfer is a record. Their
  canvas draws them identically, and this whole package exists because that
  distinction is the one that gets lost. If it is added here, a drawn edge has
  to *look* asserted and carry who asserted it --- which is the rule
  `Attribution` already enforces, applied to the picture.

The same reasoning applies to their per-node memo. It is free text with no
author and no timestamp on the face of it, which is fine for a scratchpad and
not fine for a record two people share. `chainscope note` carries the author,
how that author was identified, and what kind of statement it is, so that a
report can say who concluded what --- see §7.

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

---

## 7. What a working investigator has that this does not

*Inferred throughout* --- these come from what forensic and compliance work
involves, not from anything observed in this codebase's use. Ordered by how
much of a practitioner's day they occupy.

**Defensibility.** The output has to survive somewhere other than a report. A
figure needs to trace back to *who* fetched *what* from *which source* at
*what time* --- `AuditLog` records queries and nothing binds a number in a
report to the query that produced it. Hashing recorded responses at export, and
carrying that hash into the artefact, is the missing link. `bundle` exists and
has never been tested for the thing it is for: a colleague opening it on
another machine with different providers and re-deriving the same answer.

**Watching, not just looking back.** Half the work is being told when something
moves. `watch/` holds the rules as pure functions and nothing runs them --- no
daemon, no schedule, no delivery. The rules were the hard part and the missing
part is the easy one, which is a bad reason for it to still be missing.

**Sanctions and screening.** OFAC publishes SDN as XML on a schedule; this
imports label files by hand. Risk scoring --- how many hops from a sanctioned
address, and through what --- is composable from the taint and graph code that
already exists, and would need saying loudly that it is a heuristic, because a
number between 0 and 100 invites being read as a measurement.

**Defensibility.** *Shipped, then found broken by an external review.* `attest`
scanned a **directory** of response files. The cache is a single SQLite file
with an `entries` table, so against a real cache it hashed zero responses,
reported every recorded query as uncached, and `--verify` was structurally
unable to detect drift. The command was a no-op in the workflow its own
docstring described.

Its tests passed throughout, because the fixture built a directory of JSON
files --- it had been written to match the assumption rather than the
interface. *Observed:* **a fixture that agrees with the code about a shape
neither has checked tests nothing.** This is the fourth time this project has
hit an assertion against an assumed interface, and the first time it survived
into shipped behaviour rather than being caught by a failing test.

Now: it reads the `entries` table, and it **refuses** rather than writing an
attestation over zero responses --- a file that looks like provenance and binds
nothing is worse than no file, because somebody would ship it. That refusal is
the check that would have caught the original defect.

**Reporting.** *Shipped.* `chainscope note` is the case narrative that did not
exist at all --- rationale attached to a claim and nothing attached to the
investigation. It is append-only, four kinds, and each note carries its author
*and how that author was identified*, because a name resolved from an OS account
is not authorship and a shared case has to be able to say which is which.
`Attribution.analyst` carries the same for claims; schema 4 puts it in the
uniqueness key, since without it two people asserting the same label collapsed
into one row and the record then said one person concluded what two had.

`chainscope report` assembles the four into one file, with two deliberate
orderings: **open questions come before the findings**, because a report
ordered the usual way reads as finished no matter how much of it is not; and
where two sources of comparable strength disagree, both are printed with the
name against each and neither is picked --- that is a judgement for a person,
and it belongs in the narrative as a `decision`, where it carries a name.

Notes live in `case.db`, not the store, and that separation is the design rather
than a detail: the store is rebuildable and `clear()` exists, and a narrative a
routine rebuild can destroy is one nobody will commit anything important to.

**Still open, and now visible because of the above:** hand-made attributions
have the same not-rebuildable property as notes and still live in the disposable
store. *Observed* while writing the separation argument --- `chainscope tag`
writes judgement into a file the tool documents as safe to delete.

Not done: DOCX. The HTML carries a print stylesheet, so browser print-to-PDF
gives the artefact people attach to an e-mail; a server-side PDF or DOCX
renderer is a large dependency for a job the browser already does.

**Exchange correspondence.** *Shipped* as `chainscope request` --- and it was
another table rather than another analyzer, as guessed. It sits in `case.db`
beside the narrative, for the same reason: neither is rebuildable from a cache.

Three refusals do the work. **Overdue is derived, never stored** --- a status
column containing "expired" is correct only if somebody remembered to run a
sweep, and computed from the deadline it is true the moment it becomes true.
**Silence is not refusal** --- a request nobody answered and one somebody
declined lead to different next moves, and only the second is a decision that
can be escalated against. **An answer needs its content** --- closing a request
without saying what came back is indistinguishable from not having read the
reply.

Status is a sequence of events rather than a column, because *when* a freeze was
confirmed is regularly the fact in dispute. A closed request cannot be reopened
by appending past the close; chasing it means a new request, so the first
exchange stays legible.

Outstanding requests appear in the report under **Not yet known**, beside the
open questions, rather than in an appendix --- waiting on an exchange is the
commonest reason a case is unfinished, and a report that files it separately
reads as more complete than it is.

Not done: no delivery. Nothing here chases a deadline or sends a reminder; the
clock is reported and the exit code is non-zero while anything is outstanding,
so `cron` or a person decides what happens next --- the same line `watch` draws.

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
