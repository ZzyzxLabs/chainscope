# chainscope

**A framework for building blockchain analysis — data access, labelling,
storage, filtering, and alerting, with the reproducibility discipline already
wired in.**

[繁體中文](README.zh-TW.md)

> ⚠️ **Alpha.** APIs will change during `0.x`. The first end-to-end path works
> (Etherscan → consolidation analysis), but there are still no recorded
> cassettes, so the provider abstractions have not yet met a real API response.
> See [Status](#status) for what is and is not built.

---

## What this is

Not a finished forensics product. A **spine you build your own on top of**.

You bring the data sources, the labels, and the questions specific to your
domain. chainscope supplies the parts that are tedious to write, easy to get
subtly wrong, and near-identical in every project of this kind:

| You need to | You get |
|---|---|
| Pull chain data | Capability-routed providers with fallback, caching, throttling, audit |
| Normalise it | One `Transfer` / `Address` / `Amount` model across EVM, UTXO, Solana, Tron |
| Label addresses | Multi-source resolver that merges conflicts without hiding them |
| Store and query it | Rebuildable SQLite store with typed filters |
| Analyse it | An `Analyzer` protocol; four techniques ship as working references |
| Alert on it | *(planned)* Watches as pure functions of a block range — no daemon to run |
| Report it | Terminal, Markdown, JSON — all preserving confidence |
| Prove it | Case bundles anyone can replay offline |

Every one of those is a plugin point, and **your extension lives in your own
repository** — adding a chain, provider, store backend, analyzer, or label
source should take one file and one test cassette, with no fork of this one.

### Where the ceiling is

Commercial platforms (Chainalysis, Elliptic, TRM) rest on a decade of
proprietary attribution built from subpoenas, exchange partnerships, and
undercover purchases. That corpus cannot be reproduced from public data, and
pretending otherwise would be the most harmful thing this project could do.

Their other three capabilities need no proprietary data at all:

| Capability | Reproducible from public data? |
|---|---|
| Cross-chain matching | ✅ A search problem: time × amount × behaviour |
| Clustering & consolidation inference | ✅ Published algorithms |
| Fund-flow tracing | ✅ Graph traversal |
| Entity attribution | ⚠️ Only from public labels plus documented heuristics |

## What this actually improves

The honest competition is not Chainalysis. It is the two things people do today.

**Versus the script you would otherwise write.** Most on-chain tracing is a few
hundred lines of `requests` and `json`. That works, and it fails in a small set
of ways that repeat in every investigation:

| Failure | What it looks like | What chainscope does |
|---|---|---|
| Float arithmetic | totals subtly wrong, and they look fine | `Amount` is exact integers; mixing symbols or decimals raises |
| Silent truncation | a provider drops rows, returns `200`, your set is one address short | traversal limits surface in `Result.warnings`; nonce checks prove a history is complete |
| Failed API → empty list | an address quietly vanishes from the analysis | providers refuse rather than return empty — "unsupported" and "no data" are different types |
| Hand-built block numbers | one wrong hex digit, four wrong timestamps, one confident wrong answer | typed queries; block numbers are integers |
| Provenance lost | six months on, nobody can say where a number came from | every response recorded, content-addressed, replayable |
| A guess becomes a fact | "probably the same entity" gets repeated until it is cited | `Confidence` and `rationale` are required, not optional |

None of these are hard problems. They are problems you have to have already had,
which is why the same script keeps getting rewritten badly.

**Versus a hosted platform.** You run this. The data is on your disk, the
provider keys are yours, there is no account, no quota, and nothing anyone can
revoke. And because a hosted platform cannot hand you the data underneath its
answers, its output is ultimately *trust us*. Yours can be checked.

**The part that is genuinely new: a case is a file you can send someone.**

```bash
chainscope bundle theft.chainscope        # what is inside, and whether it replays
```
```python
cache = Bundle.open("theft.chainscope").replay_cache()    # offline, no API keys
```

A bundle carries the results *and every raw provider response that produced
them*. A reviewer reruns the analysis offline and gets byte-identical output, so
a disagreement becomes "your log query missed block 20011451" rather than "I do
not believe you". The same mechanism keeps the test suite offline, and keeps an
investigation intact after the provider that answered it shuts down. Commercial
platforms structurally cannot offer this: their data is not permitted to leave.

## Run your own

chainscope is a toolkit, not a service. The intended end state is that you build
your own system on top of it and keep running it without us.

| You want | What you get |
|---|---|
| Your own database | `Store` protocol — SQLite by default, Postgres/DuckDB/graph as plugins. Rebuildable from the cache, so a schema change is a rebuild, not a re-crawl |
| Your own frontend | Stable JSON for every `Result`, graph export to Neo4j/Gephi/Cytoscape, a read-only local API. No bundled UI you have to live with |
| Your own alerts | `Watch` + `evaluate(watch, since, until)`, a pure function over a block range. Drive it from cron, CI, or your own daemon. No scheduler, no broker — and because it is pure, *"why did this fire?"* is answered by replaying it |
| Your own analysis | Analyzers, providers, chains, stores and attribution sources are entry points. Your extension lives in your repository |
| Your own labels | `Attribution` carries source, method, confidence and rationale, and merges without destroying conflicts — a shared label set you can argue with instead of merely trust |

Ingestion follows the investigation instead of indexing whole chains, which is
what keeps this a laptop tool rather than a cluster.
[ARCHITECTURE.md](ARCHITECTURE.md) §4.8–4.11 gives the reasoning for each.

## The design commitment that matters

Blockchain forensics has a recurring failure mode. A heuristic guess gets
written down, passes through three tools, and emerges looking like a fact.
People get accused on that basis.

chainscope makes that structurally hard. Every attribution carries its
provenance and its evidentiary weight, and the type system refuses to let you
omit them:

```python
Attribution(
    address="bc1q...",                 # a swap service's Bitcoin hot wallet
    label="Instant-swap service (BTC side)",
    category=Category.CEX,
    confidence=Confidence.LOW,         # ← behavioural inference, not a label
    method=Method.INFERENCE,
    source="analyst",
    rationale="Payouts land 5-45 min after deposits at a consistent discount "
              "to spot, always to a fresh address with change returning here.",
)
```

Omit the `rationale` on a low-confidence claim and construction fails. Anything
below `Confidence.HIGH` renders as a claim, not a label. Sanctions hits can
never be buried under a friendlier label during merge.

This is not defensive boilerplate. It is the professional ethic of the field,
enforced by the compiler instead of by whoever happens to be reading.

## What it does

Nine analyzers, and the number beside each is where it stops working. That
column is the point: a technique with no measured failure boundary is a
technique that answers confidently in cases it cannot handle.

| `chainscope analyze …` | Answers | Where it breaks |
|---|---|---|
| `taint` | how much of this balance came from that theft | FIFO depends on arrival order, so a clipped window changes *which* funds paid for what |
| `mixer` | which withdrawal matches this deposit | precision 100% / 57% / 33% / **8.3%** at 0 / 1 / 2 / 4 competing withdrawals; refuses past 5 |
| `probing` | did they send a test payment first | needs 5 increasing steps **and** 8× growth — length alone fires on **38%** of ordinary accumulation |
| `common_funder` | which addresses share an origin | an exchange funds its customers: without the service guard, precision **0.7%** |
| `co_spend_cluster` | which addresses share a wallet (UTXO) | one CoinJoin halves precision |
| `temporal` | what hours the operator keeps | needs 30 timestamps, and **refuses** when the plausible band spans most of the clock |
| `peel_chain` | follow a peel chain | halts on a contested or missing hop rather than guessing |
| `cross_chain` | the far side of a swap | **ranks a decoy first when the true payout is absent** |
| `consolidation` | where an address's counterparties send funds | — |

Also, reachable from Python and the agent: reverse taint (what funded this
balance), bytecode family comparison (same contract, new address?), one-hop
relay detection, revenue-split analysis (who takes a fixed cut), memo
authorship, and historical token decimals.

**Getting data in and out**

- **Label** one address from the CLI, import somebody's CSV or JSON with the
  columns it already has, tag from the browser extension, or let an agent do it
  — every path records where the claim came from and cannot be told to lie
  about that.
- **Query** with `chainscope sql`, a Dune-shaped surface over DuckDB with exact
  128-bit arithmetic. `--schema` documents the traps, not just the columns.
- **Store** is one SQLite file. A case is a file you can copy.

**Seeing it**

- `graph -f flow` lays the money out left to right by hop. Click an address for
  *every* route from the seed, click `+n` to open one more ring, drag the
  slider to watch the case develop. Dashed boxes are frontier — seen, never
  expanded.
- **It is a canvas, not an export.** Hide what you are not working on, name a
  node in your own words, drag it — then save it as JSON and load it into a
  wider re-run of the same graph. State is keyed by address, so it survives.
  The header says how many nodes you hid: a picture quietly missing things
  looks complete and is not. A name you typed is marked as yours and is never
  shown as an attribution.
- Case notes appear on the node they are about, **with their author** —
  `chainscope note observation "…" --about 0xabc`.
- `dashboard` for a case overview that states the unattributed share out loud.

**Writing the case down**

- `note` records the reasoning — observations, decisions, open questions, and
  corrections — append-only, each carrying its author *and how that author was
  identified*, because a name resolved from an OS account is not authorship.
- `report` assembles narrative, claims, coverage and provenance into one file.
  **Open questions print before the findings**, and where two sources of equal
  strength disagree both are shown with a name against each and neither is
  picked. HTML with a print stylesheet, or Markdown.
- `attest` binds the figures to the cached responses behind them; `--verify`
  reports drift. A manifest, not a signature — and it says so.
- `request` is the clock on what was asked of an exchange — KYC, freeze,
  records, preservation. Overdue is computed from the deadline rather than
  stored, and **silence is not recorded as refusal**: only the second is a
  decision somebody made, and only the second can be escalated against.

**Interfaces**

CLI (12 commands) · MCP agent (10 tools, including writes) · MV3 browser
extension · third-party analyzers via entry points · Docker, Nix flake, and
uvx.

See [`docs/demo.md`](docs/demo.md) for an eight-minute walkthrough.

## Install

```bash
pip install chainscope            # core
pip install "chainscope[all]"     # + EVM, Bitcoin, Solana, Tron, rich output
```

## Status

v0.1 is being built. Implemented so far:

- [x] `core.units` — exact `Amount` arithmetic, no floats, ever
- [x] `core.chainid` — CAIP-2 chain identity
- [x] `core.attribution` — provenance, confidence, non-destructive merge
- [x] `transport` — content-addressed cache, finality-derived TTL, throttling, audit log
- [x] `providers` — capability-routed data sources with fallback
- [x] `chains` — EVM, Bitcoin, Solana, Tron adapters
- [x] `attribution` — OFAC, explorer nametags, local labels, conflict resolution
- [x] `analysis` — consolidation, cross-chain matching, peel chains, clustering
- [x] `cli` and renderers — terminal, Markdown, JSON
- [x] `case` — replayable bundles, and an append-only case log with per-analyst
      authorship (`note`, `report`)
- [x] `watch` — `evaluate()` over a block range, with a runner ([§4.10](ARCHITECTURE.md))
- [x] `store` — entity store with typed filtering, rebuildable from the cache
      ([§4.8](ARCHITECTURE.md))
- [x] `providers.etherscan` — explorer-class `ADDRESS_HISTORY` and
      `ASSET_TRANSFERS`, one key across 60+ EVM chains. This is what makes the
      bundled analyzers runnable end-to-end

Designed, not yet built. The reasoning is written down first on purpose: these
are the decisions that are expensive to reverse once anyone depends on them.

- [ ] recorded cassettes — the provider abstractions have not yet met a real
      API response, so their shapes are unvalidated
- [ ] fetchers for attribution sources — today they read local files you supply
- [ ] plugin protocol versioning and stability tiers ([§4.11](ARCHITECTURE.md))
- [ ] `analyze --bundle` — one-command replay; today it is `Bundle.replay_cache()`
- [ ] graph export, local read API

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and roadmap.

## Extending

Adding a chain, provider, store backend, analyzer, or attribution source should
take **one file and one test cassette**, and you should never need to fork this
repository. That is the benchmark the architecture is measured against.

Register via entry points; no core changes:

```toml
[project.entry-points."chainscope.analyzers"]
my_analyzer = "my_package:MyAnalyzer"
```

See [docs/extending.md](docs/extending.md).

## Read-only by construction

The transport layer has no signing capability — not by policy, by type. Query
types form a closed union of read operations, and a method allowlist blocks
`eth_send*`, `eth_sign*`, and `personal_*` at the wire.

A forensics tool that can move funds is a liability. This one cannot.

## ⚠️ Heuristics are not evidence

Clustering, cross-chain matching, and change-output detection produce
**hypotheses with scores**, not conclusions. Results below `Confidence.HIGH`
must not be used to accuse any person or entity without independent
verification.

If you are building a case that affects someone's liberty or livelihood, this
tool helps you find leads. It does not close them.

## License

Apache-2.0. Attribution datasets are distributed separately under their own
terms; see `docs/data-sources.md`.
