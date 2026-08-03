---
name: chainscope
description: Blockchain forensics — trace funds across chains, label addresses with provenance, query a local store with SQL, export fund-flow graphs, run watch rules, and keep a case record with narrative, per-analyst authorship, and exchange correspondence. Use when investigating on-chain activity, tracing stolen funds, attributing addresses to services, writing up or defending a case, or answering questions about transfers, balances, and counterparties.
---

# chainscope

A framework for blockchain investigation. Everything is local: a SQLite store
for traversal, a DuckDB view for analytics, and recorded responses so an
analysis can be replayed without network access or API keys.

## The one rule that matters

**Never state an attribution without its confidence and source.** Every claim
this tool returns carries both, and dropping them turns a MEDIUM-confidence
nametag into a statement of fact. Write "labelled Binance 14 (HIGH confidence,
source: explorer nametag)" rather than "this is Binance".

Two more failure modes to watch for, because they read as findings when they
are not:

- **An empty result is not evidence of absence.** "No attribution found" means
  nobody has labelled it *in this store*. Say that, not "it is unlabelled" and
  never "it looks clean".
- **A truncated result is not a complete one.** Anything reporting
  `truncated: true` or a `frontier` count is a partial view. Say so before
  summarising it.

## Amounts

Every amount is an exact integer in the asset's smallest unit — wei, satoshi,
MIST. They routinely exceed what a float holds exactly (10 ETH is 1e19; a
double is exact only to ~9e15), so:

- Never convert one to a float, and never let one round.
- Decimals differ per asset: ETH 18, USDC 6, SUI 9. Divide by the asset's own
  `decimals`, never by an assumed 18.

## Common tasks

### Start here when you have one address and no plan

```bash
chainscope investigate 0xSUSPECT -c eth
```

Runs what applies, says what came back, and **names the next command with its
arguments filled in**. Use it before reaching for a specific analyzer: twelve
analyzers each need parameters somebody has to already know, and this is the
step that produces them.

It exits non-zero when nothing was found, so silence is not readable as a clean
bill of health. An empty step means *this window held no evidence of that
pattern*, never *the pattern is absent*.

### Record somewhere to look next, and what came of it

```bash
chainscope lead scan 0xADDR                       # read its ENS entry for leads
chainscope lead scan 0xADDR --apply               # and file what survives
chainscope lead add 0xADDR -k twitter -v alice -s "ENS text record on foo.eth"
chainscope lead list --open                       # what is still outstanding
chainscope lead settle 1 --verdict refuted --why "that account never published it"
```

**A lead is not an attribution and the distinction is load-bearing.** An ENS
text record reading `com.twitter = alice` does not mean the address belongs to
@alice; it means whoever controls that name typed "alice" into a field. Every
lead carries the specific step that would confirm it, and that step is always
something a *person* does somewhere this tool cannot reach.

`scan` reads text records **only from a forward-confirmed name** --- one that
resolves back to the address it claims. An unconfirmed reverse record is a claim
by whoever owns the name, about somebody else, so its text records are *their*
handles; filing them would attach another person's identity to this address.
They are not even fetched.

Two behaviours to rely on:

- **Refuted leads are kept, never deleted.** The record that somebody already
  checked is what stops you repeating the search --- and in a shared case, stops
  two people doing it at once. Re-filing a settled lead tells you it was
  settled, and by whom, and why.
- **A verdict without a reason is refused.** "Confirmed" with no stated basis
  reads, once its author has moved on, exactly like a guess made quickly.

### Look up what is known about an address

```bash
chainscope label 0x28C6c06298d514Db089934071355E5743bf21d60 --local labels.json
```

Report every claim it returns, including ones that disagree. Disagreement
between sources is usually the interesting part, not a data-quality problem.

### Record a label

```bash
# One address
chainscope tag 0xABC… -l "eXch deposit" -t cex -C high -s "reverse-consolidation analysis"

# A file somebody already has (csv/json/jsonl); columns are auto-mapped
chainscope tag labels.csv -s "team-labels"          # dry run, reports problems
chainscope tag labels.csv -s "team-labels" --apply  # writes
```

`--source` is required and there is no way around it. A confidence of `low` or
`speculative` also requires `--why`; state the actual evidence, not that it
seems likely.

Always dry-run a file import first and read the rejected rows — they are
usually a column-mapping mistake, not bad data.

### Run an analysis

```bash
chainscope analyze --list                       # what is installed
chainscope analyze taint -p source=0xTHIEF      # where the stolen value sits now
chainscope analyze probing -p address=0xOP      # did they test the route first
chainscope analyze temporal -p address=0xOP     # what hours do they work
chainscope analyze impersonation -p address=0xV # which of these tokens are fake
chainscope analyze poisoning -p address=0xV     # which of these addresses are traps
chainscope analyze route -p source=0xA -p target=0xB   # how could A have reached B
```

Twelve analyzers ship. Pick by the question, and **quote the qualifier each one
carries** --- these numbers are measured, and they are the difference between a
finding and a coincidence.

| analyzer | answers | when it stops working |
|---|---|---|
| `taint` | how much of a balance came from a source | FIFO depends on arrival order, so a clipped window changes *which* funds paid for what |
| `probing` | did they send a test payment first | needs 5+ strictly increasing steps **and** 8x growth; length alone fires on 38% of ordinary accumulation |
| `mixer` | which withdrawal matches this deposit | precision 100% / 56.7% / 33.3% / 8.3% at 0 / 1 / 2 / 4 competing withdrawals; refuses past 5 |
| `common_funder` | which addresses share an origin | an exchange funds its customers: with the service guard off, precision drops to 0.7% |
| `co_spend_cluster` | which addresses share a wallet (UTXO) | one CoinJoin halves precision |
| `temporal` | what hours the operator keeps | needs 30+ timestamps; a scripted address has no timezone to report |
| `peel_chain` | follow a peel chain | halts on contested or missing hops rather than guessing |
| `cross_chain` | the far side of a swap | **ranks a decoy first when the true payout is absent** |
| `consolidation` | where counterparties send funds | — |
| `impersonation` | which assets claim a symbol that is not theirs | "unlisted" is not clean --- most tokens are in no registry, and the check simply had nothing to compare against |
| `poisoning` | which counterparties were ground to resemble another | refuses to nominate the real one unless a *trusted* asset shows the subject paying it |
| `route` | how money could have got from A to B | withholds routes through a high-degree address by default: a custodian commingles, so that link is in the ledger, not in the money |

**Run `impersonation` before quoting any per-symbol total.** Measured on a real
case: 42 of an address's 55 ERC-20 transfers belonged to tokens imitating USDC
and ETH, so a total grouped by symbol was mostly forgery --- and it looked
exactly like a real number, same units, same magnitude, same place in the
report. It is the one place in a case where the data is *chosen by the
adversary*, having looked at the tool that will render it.

Three mechanisms, and no two overlap, so no single check finds all three:

| symbol | how it works | what catches it |
|---|---|---|
| `UЅDC` | Latin, one Cyrillic letter spliced in | mixed-script (UTS #39 §5) |
| `ЕТН` | *entirely* Cyrillic, so perfectly consistent | confusable skeleton (§4) |
| `ETH` | plain ASCII, simply named after a real one | **contract address only** |

The third is the one Unicode cannot touch, and the reason the rule is *compare
the contract, never the symbol string*.

**`poisoning` is the same attack aimed at the address instead of the ticker**,
and the two must be run together. An attacker grinds an address matching the
first four and last four characters of a real counterparty, sends a zero-value
transfer so it lands in the history, and waits for somebody to copy it. In the
same case, 9 of 36 counterparties fell into such a group --- against a
coincidence probability of 1.5e-7. Quote that number: "these look similar"
invites "coincidences happen", and they do, at a rate the finding states.

**Read the `hypotheses`, not only the `findings`.** That a group exists is
arithmetic and is reported as a finding. *Which member is genuine* is an
inference, it is capped at MEDIUM, and its score factors are listed so you can
disagree with one instead of with the conclusion. The same split applies to
`impersonation`: a contract that is not the canonical one for its symbol is a
finding --- the chain settles that --- while "this string renders like that
string" is a hypothesis.

It will often say **it cannot tell which one is real**, and that is the feature.
A token contract emits its own transfer events, so a forged token can log a
payment the victim never made --- 24 of the 27 addresses in a lookalike group
there appeared *only* in forged-token transfers. Evidence from an asset that
fails the impersonation check counts for nothing, because naming the wrong
address is how somebody's next payment reaches the attacker.

**`route` answers "how did A get to B", and every route it returns respects
time.** That is not a detail. A plain hop-count search has no notion of when
anything happened, so it returns paths where a hop occurs *before* the money
arrived --- measured on a real ledger, **62% of its multi-hop answers were
causally impossible**. Asked the other way, 53% of the address pairs it calls
connected are not.

Two things to quote whenever you show a route:

- **What its narrowest hop could carry.** A route whose thinnest link moved
  0.001 ETH is not how 1,000 ETH travelled.
- **Whether it crosses a hub.** Routes through a high-degree address are
  withheld by default and counted. A custodian commingles what it receives, so
  such a route is a link in the ledger and not a link in the money --- and with
  hubs allowed, almost any two addresses on a chain are "connected".

And **no route found is not proof of no connection**: funds that crossed a
chain, an exchange, or an address the store has never seen leave no chain
behind.

Four things to carry into any summary:

- **`taint` separates holding from having-touched.** "Stolen value passed
  through here" and "this address holds stolen value" are different claims.
  Reporting the second as the first is how a payment processor gets described
  as a launderer. The result names them separately; keep them separate.
- **`mixer` never exceeds MEDIUM on timing** --- it is a claim about operator
  behaviour, not a break of the cryptography. But `address_reuse` (the same
  address deposited *and* withdrew) is HIGH and ONCHAIN, because nothing is
  being inferred. Check that one first; it does not decay as the pool gets busy.
- **`probing` describes a shape.** A trading desk scaling into a position looks
  identical. Say what was observed, not what it means.

### Value something at the time it moved

```bash
chainscope value 12.4 --symbol ETH --at 2022-04-06        # one amount
chainscope value 0xSEED --quote USDT                      # an address's flows
```

**At the time, never now.** A figure at today's rate is a different claim from
one at the rate when the money moved, and only the second is defensible. Every
valuation prints the rate, the moment, and the source; quote all three.

Three refusals to carry into any summary:

- **No rate for that minute means no figure** --- not the nearest one. If the
  output says a transfer could not be valued, say so; do not fill it in.
- **An undated transfer is not valued at all.** A provider omitting a timestamp
  is not evidence the transfer happened today.
- **The total is a floor when anything was refused**, and the command says so.
  It is also *a sum of valuations, not a valuation of a sum* --- each transfer
  converted at its own moment. Never restate it as "worth X today".

### Cross-check an enumeration

Any query whose answer is a *set* --- all logs in a range, every transfer of an
address --- can come back silently short. A provider returning 200 OK with
twelve of thirteen rows looks exactly like one returning all thirteen, and this
has cost a real investigation a missing address.

`Router.corroborate` asks two independent providers and reports what only one
of them saw. Blockscout needs no API key, so a second source exists by default
on six EVM chains. When a result says `corroborated: false`, say so --- it means
one source answered, which is a weaker claim than it looks.

### Export a fund-flow graph

```bash
chainscope graph 0xSEED --out case.html --depth 2 --max-nodes 150

# -f flow lays it out in columns by hop distance instead of a spring layout,
# so a laundering chain reads as a chain rather than as a blob. Dashed nodes
# are frontier: seen, never expanded.
chainscope graph 0xSEED -f flow --out flow.html
chainscope graph 0xSEED -f dot | dot -Tpng -o case.png
```

The HTML is self-contained: no server, no CDN, opens from a file path. Dashed
nodes are the frontier — seen but never expanded. If the command reports
`truncated`, say so when describing the result.

### Render a case overview

```bash
chainscope dashboard --out case.html
```

Counts, coverage, largest flows, and what is *not* attributed. Read the
unlabelled figure aloud when summarising: a case where 80% of addresses carry
no label is a case where most of the picture is unexamined, and a dashboard
that looks tidy does not change that.

### Query with SQL

The analytical view exposes `transfers(chain, tx_hash, sender, recipient,
amount_raw, decimals, symbol, asset, kind, block, timestamp)` and
`attributions(address, chain, label, category, confidence, method, source,
rationale)`.

`amount_raw` is a 128-bit integer, so `SUM()` is exact. Reads only — writes,
file access, and chained statements are refused.

```bash
chainscope sql --schema                     # the columns, and the traps in them
chainscope sql "SELECT asset, symbol, decimals, SUM(amount_raw)
                FROM transfers GROUP BY asset, symbol, decimals"
```

**Group by `asset`, not `symbol`.** A symbol is a label anybody can reuse, and
a scam token calling itself USDC is routine. Measured: grouping by symbol
summed 5,000 real USDC (6 decimals) with 1,000 impostor tokens (18 decimals)
into one number denominated in nothing. `asset` is the contract, which is the
identity; carry `decimals` with it, because a total is meaningless without the
scale it is in.

```python
from chainscope.store.analytics import AnalyticsView
view = AnalyticsView(":memory:")
view.build_from_sqlite(".chainscope/store.db")
view.sql(
    "SELECT asset, symbol, decimals, SUM(amount_raw) FROM transfers "
    "WHERE sender = ? GROUP BY asset, symbol, decimals",
    [addr],
)
```

Read `chainscope sql --schema` before writing a query. It documents what will
mislead you, not only what the columns are called.

### Evaluate a watch rule

```python
from chainscope.watch import Watch, TouchesCategory, evaluate
from chainscope.core.attribution import Category

w = Watch(name="mixer-exposure", subject=addr, chain=ETHEREUM,
          predicate=TouchesCategory(Category.MIXER))
events = evaluate(w, store, since=18_000_000, until=18_100_000)
```

Pure over a block range — no scheduler, no clock. The same range always gives
the same events, which is what makes "why did this fire?" answerable later.

To run rules from a file rather than from Python:

```bash
chainscope watch rules.json --every 300      # loop; omit --every to run once
```

**A watch that could not run is not a watch that found nothing.** When a rule
fails to evaluate, its saved position is *not* advanced, so the gap does not
silently become a period nobody watched. There is no delivery: events go to
stdout and an exit code, and cron or a person decides what happens next.

### Refresh the sanctions snapshot

```bash
chainscope sanctions --check      # what would change, writes nothing
chainscope sanctions             # fetch OFAC SDN and write the snapshot
```

Screening reads the **snapshot**, not the network, so a report can say which
snapshot it was screened against. The diff is the output: a removal is reported
as a *delisting*, which means the opposite of a deletion for an address sitting
in an open case. If parsing yields zero addresses it refuses to overwrite —
that means the format moved, not that sanctions were lifted.

### Keep the case record

The narrative, the authorship, and the clock. All of it lives in `case.db`,
which is **separate from the store on purpose**: the store is rebuildable from
the cache and safe to delete, and nothing a person wrote is either of those.

```bash
export CHAINSCOPE_ANALYST="you@example.com"   # who is asserting things

chainscope note observation "0xabc funds three of the four drainers"
chainscope note decision    "not tracing past the CEX deposit; terminal"
chainscope note question    "who paid the gas for the first probe?"
chainscope note correction  "the 3rd hop is a router" --supersedes 4
chainscope note --open                        # what nobody has answered
```

Four kinds, not free text, and **append-only**: a note that was wrong is
superseded rather than edited, and both stay readable. When summarising a case,
read `chainscope note --open` — a case record listing only conclusions reads as
finished no matter how much of it is not.

Set `CHAINSCOPE_ANALYST` before writing anything. Without it the tool falls back
to git's e-mail and then the OS account, and an OS account is **not** authorship
— claims made that way are recorded with *no* analyst rather than signed with a
machine login.

```bash
chainscope request send "Binance" -k freeze --about 0xabc \
    --sent 2026-07-02 --due 2026-07-09 --ref TICKET-9912
chainscope request update 3 answered --note "12.4 ETH held"
chainscope request list --open
```

The clock on what was asked of an exchange. **Silence is not refusal** — a
request nobody answered and one somebody declined are different facts, and only
the second can be escalated against. Never report an unanswered request as a
denial. Overdue is computed from the deadline, so it is true the moment it is
true.

```bash
chainscope attest                 # hash the cached responses a case rests on
chainscope attest --verify        # exits 1 if any of them moved
chainscope report --title "Case 2026-114" --attach flow.html --out case.html
```

`attest` is a **manifest, not a signature**: it catches drift and accident, not
somebody with write access to the case directory. Say that if you cite it.

`report` puts the narrative, the claims, the coverage and the provenance in one
file. Two things in it to preserve when you summarise: open questions and
outstanding requests print **before** the findings, and where two sources of
comparable strength disagree both are shown with a name against each and
**neither is picked**. Do not pick one for the reader.

### Hand the whole case to somebody else

```bash
chainscope bundle theft.chainscope      # what is inside, and whether it replays
```

A bundle carries the results *and every raw provider response that produced
them*, so a reviewer reruns the analysis offline with no API keys and gets
byte-identical output. A bundle you received is untrusted input — it was
produced by somebody else.

### Serve to another agent over MCP

```bash
chainscope-mcp --store .chainscope/store.db              # read-only
chainscope-mcp --writable --agent-name my-agent          # may also label
```

Writing is off by default. When on, labels are recorded as
`agent:<name>` with method `inference`, and notes as `agent:<name>` with an
identity source of `agent` — so a human can later tell a model's suggestion
from their own judgement.

Twelve tools. `case_record` is the one to call before summarising anything: it
returns the open questions and the requests still waiting on a reply, which is
what a case record made only of conclusions leaves out.

The agent can write notes and labels. It deliberately **cannot** record that a
freeze request was sent — that is an action taken outside the tool, by a person,
and a model asserting it happened would put a fact into the case record that
nothing backs.

## Setup

```bash
pip install -e ".[all]"
cp .env.example .env      # then fill in what you have
chainscope doctor         # reports what is configured and what it unlocks
```

The key worth having is `ETHERSCAN_API_KEY` — free, covers 60+ EVM chains, and
is the only way to answer "what did this address do" on EVM. No JSON-RPC method
lists an address's transactions.

Sui needs no key at all: `suix_queryTransactionBlocks` filters by address
directly.

## Reading the numbers correctly

| Situation | What it means | What to say |
|---|---|---|
| `claims: []` | Nobody labelled it in this store | "no attribution in this store" |
| `truncated: true` | A limit stopped the query | "partial — there is more" |
| `frontier: 12` | 12 addresses seen, never expanded | "the graph stops here by choice" |
| `confidence: MEDIUM` | A guess with reasoning | quote the confidence |
| `ResultTruncated` raised | API returned exactly the limit | any total is a lower bound |

## What this cannot do

- It does not de-anonymise anyone. It attributes addresses to *services* from
  labels and on-chain behaviour.
- It is read-only by construction: signing and broadcasting are blocked in the
  transport layer, not by convention.
- Label coverage is only as good as the sources loaded. Absence of a label is
  never evidence of anything.
