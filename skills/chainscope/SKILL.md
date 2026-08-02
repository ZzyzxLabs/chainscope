---
name: chainscope
description: Blockchain forensics — trace funds across chains, label addresses with provenance, query a local store with SQL, export fund-flow graphs, and evaluate watch rules. Use when investigating on-chain activity, tracing stolen funds, attributing addresses to services, or answering questions about transfers, balances, and counterparties.
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
```

Nine analyzers ship. Pick by the question, and **quote the qualifier each one
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

Three things to carry into any summary:

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

```python
from chainscope.store.analytics import AnalyticsView
view = AnalyticsView(":memory:")
view.build_from_sqlite(".chainscope/store.db")
view.sql("SELECT symbol, SUM(amount_raw) FROM transfers WHERE sender = ? GROUP BY symbol", [addr])
```

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

### Serve to another agent over MCP

```bash
chainscope-mcp --store .chainscope/store.db              # read-only
chainscope-mcp --writable --agent-name my-agent          # may also label
```

Writing is off by default. When on, labels are recorded as
`agent:<name>` with method `inference`, so a human can later tell a model's
suggestion from their own judgement.

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
