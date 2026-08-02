# Roadmap

Where chainscope is going, and — more usefully — the measurements behind each
decision. Anything here stated as a fact was measured; anything stated as a
guess says so.

## What this is aiming at

A framework other people build their own tooling on: analysis, labelling,
storage, filtering, alerting. Not a finished product with a plugin slot bolted
on, but a set of layers each of which is useful alone and replaceable
individually.

The test of that is concrete: **someone should be able to clone this, point it
at their own data source, define their own labels, and have a working
investigation loop the same afternoon** — without reading the internals.

---

## Decisions already settled by measurement

### Storage: SQLite for traversal, DuckDB for analytics

Two million transfers, 200,000 addresses, power-law degree distribution,
wei-scale amounts. Median of repeated runs on an M-series laptop.

| Query shape | SQLite | DuckDB (indexed) |
|---|---:|---:|
| `edges(address)` — the traversal primitive | **0.7 ms** | 6.3 ms |
| `edges(address)` — cold address | **0.04 ms** | 0.4 ms |
| Filtered scan over a time window | 669 ms | **2.7 ms** |
| BFS depth 3 | 280 ms | **68 ms** |
| Exact `SUM` over a token | 277 ms | **5.4 ms** |
| Bulk ingest of 2M rows | 5.7 s | **2.3 s** |
| On-disk size | 769 MB | **191 MB** |

The engines win at opposite things, and both shapes are load-bearing here, so
the answer is not "pick one":

* **SQLite stays the write path and the traversal store.** `edges()` runs
  constantly — every graph walk, every expansion, every hop — and a B-tree
  point lookup is an order of magnitude better at it than a columnar scan.
* **DuckDB becomes a derived analytical view.** Scans, aggregates, dashboards,
  and the ad-hoc SQL surface. This fits the existing rule that a store is
  rebuildable from the cache (ARCHITECTURE §4.8): the DuckDB view is one more
  derived index, not a second source of truth.
* **`DuckDB ATTACH` over the SQLite file** is the zero-rebuild escape hatch for
  exploratory SQL. Measured at ~95 ms for the same queries — 35× slower than
  native DuckDB and 160× slower than SQLite on point lookups, because the
  scanner pushes no indexes down. Fine for a human typing a query, wrong for
  anything on a hot path.

One number is worth stating on its own. The exact sum of the ETH column was
`3,001,703,404,365,735,323,442,286,940` — **325 million times larger than
SQLite's `INTEGER` maximum**. That is why amounts are zero-padded text in
SQLite and summed in Python today, and why DuckDB's 128-bit `HUGEINT` matters:
it does the same sum in-engine, exactly, 50× faster. Both agreed to the digit.

> A caution about the first run of this benchmark: it reported DuckDB ingest at
> 777 s against SQLite's 4.4 s, which would have argued for the opposite
> conclusion. That was row-at-a-time `executemany` — DuckDB's worst path — not
> a property of the engine. Bulk ingest via `COPY` is 1.05 s. The corrected
> numbers are above.

### Language: Python for the framework

The intuition that blockchain analysis is compute-heavy does not survive
measurement.

| | |
|---|---:|
| Building one `Transfer` from parsed JSON | 2.1 µs |
| 200,000 of them | 0.46 s |
| Fetching those 200,000 at a real explorer rate limit | 6.7 s |
| **Fetch : parse** | **~15 : 1** |

A rewrite in Go or Rust removes the parse column. It cannot touch the fetch
column, which is set by somebody else's rate limit — and that limit was
measured at **3 requests/second** on Etherscan's free tier, not the documented
5. Everything else that could be CPU-bound already runs in C++ inside SQLite or
DuckDB.

So Python stays, for the reason that matters most to this project: the people
expected to write analyzers, label sources, and providers are analysts and
researchers, and that is where they already are.

**The case would change** if interactive traversal over tens of millions of
edges becomes the bottleneck. If profiling ever shows that, the answer is to
extract *that one component*, not to rewrite the framework.

---

## Where the gaps actually are

Assessed against seventeen real investigations spanning EVM, Bitcoin, and Tron:
mixer tracing, cross-chain swaps, multi-chain contract deployment, storage
forensics on unverified contracts, and token accounting audits.

**In reasonable shape.** Explorer-backed address history; archive `eth_call` at
a fixed block; `getLogs` over ranges; cross-chain matching; peel chains;
consolidation and reverse-consolidation; co-spend clustering; price lookup at a
timestamp; provenance and confidence in the type system; case bundles.

**The real gaps**, roughly in order of how much they block everything else:

| Gap | Why it blocks | Status |
|---|---|---|
| Analytical query layer | Dashboards, SQL, graph views all read through it | planned first |
| Graph export | Nothing can be visualised without it | not started |
| Labelling ingest path | The framework's core promise; needs a store to write to | not started |
| Agent surface (MCP) | Natural-language search and labelling | not started |
| Watch / alerting | Documented in ARCHITECTURE §4.10, never implemented | not started |
| Non-EVM providers | Chain modules exist; nothing fetches for them | Bitcoin, Tron, Solana, Sui |
| Multi-chain address correlation | Same contract at one address across chains | not started |
| OSINT correlation | Attribution beyond on-chain evidence | not started |

Note the shape of that list: the analysis layer is the *least* of the problem.
What is missing is everything around it that turns analysis into a tool other
people can build on.

---

## Order of work

Chosen by dependency, not by appeal. Each item is useful on its own — the point
is that nothing here is a rewrite waiting on a later stage to pay off.

**1. Analytical query layer.** A DuckDB view derived from the store, rebuildable
and disposable. Delivers the SQL surface, and is the thing dashboards and graph
views read.

**2. Graph export.** Node/edge extraction with attribution attached, in formats
existing visualisation tools already read, before writing any UI.

**3. Labelling ingest.** CLI and library path for getting labels in, with the
provenance rules already enforced by `core/attribution.py`. Bulk import, source
tracking, conflict handling.

**4. Agent surface.** MCP server over the query and labelling layers, so an
agent can search, label, and traverse without a bespoke integration.

**5. Watches.** ARCHITECTURE §4.10 specifies them as pure functions over the
store. Implement as specified.

**6. Chain coverage.** Bitcoin and Tron providers first — both have real
investigations behind them. Sui next. Solana after.

**7. Visual layer.** Only once 1–3 exist. A fund-flow view that cannot show
provenance and confidence would contradict the rest of the project.

Unscheduled and honestly uncertain: OSINT correlation, multi-chain address
correlation, and third-party tool integrations. All are worth doing; none is
close enough to specified to put a number on.

---

## Open questions

* **Does the DuckDB view stay in sync, or get rebuilt?** Rebuilding is simpler
  and consistent with §4.8. It is also slower for a long-running session. Not
  yet decided; wants a measurement of rebuild time at realistic case size.
* **Taint propagation policy.** Poison, haircut, and FIFO give materially
  different answers to "is this address tainted", and most tools do not say
  which they use. Making the choice explicit fits this project's stance on
  provenance, but the algorithms need validating against known cases before
  claiming anything.
* **Where labels come from at scale.** The provenance rules are enforced; the
  supply of good labelled data is the actual constraint, and it is a data
  problem rather than a code one.
