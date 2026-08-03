# Architecture

Why chainscope is shaped the way it is. Read this before proposing a change to
a public interface.

---

## 1. What this is, and is not

**Goal:** implement the parts of blockchain forensics that public data supports,
and be honest about the parts it does not.

Commercial analytics platforms rest on a proprietary attribution corpus built
over a decade from subpoenas, exchange partnerships, and undercover purchases.
That corpus cannot be reproduced from public sources, and pretending otherwise
would be the single most harmful thing this project could do.

Everything else is tractable:

| Capability | Public-data reproducible | Why |
|---|---|---|
| Cross-chain matching | Yes | A search over time × amount × behaviour |
| Clustering & consolidation | Yes | Published algorithms over public graphs |
| Fund-flow tracing | Yes | Graph traversal |
| Entity attribution | Partly | Public labels plus documented heuristics only |

### Non-goals

- Rebuilding a proprietary attribution database, or implying we have one
- Any write path to a chain — see §4.2
- **Running a hosted service.** chainscope is something you run, not something
  you log into. That is a positioning decision rather than modesty: hosting puts
  the RPC quota, the uptime, and the abuse handling on us, and each of those
  costs bends the design toward whatever keeps the service cheap. Self-hosting
  puts them on the party that benefits, and keeps case data on their machine —
  frequently a hard requirement for the people this is for. See §4.11
- **A scheduler, a daemon, or a message bus.** Alerting *is* supported, but as a
  pure function the caller invokes — see §4.10
- Indexing whole chains. Ingestion follows the investigation — see §4.9
- Anything where latency is the value. Sub-second freshness and reproducible
  history are opposing design targets; this project has chosen the second
- Producing regulatory filings — compliance liability should not sit in a tool

---

## 2. Domain model

Every layer speaks the same immutable value objects:

| Type | Purpose |
|---|---|
| `Amount` | Exact quantity: raw integer + decimals + symbol. Never a float. |
| `ChainId` | CAIP-2 identity: `eip155:1`, `bip122:…`, `solana:…` |
| `Address` | Chain-scoped address preserving case semantics |
| `Transfer` | Normalised value movement: from, to, amount, asset, tx, kind |
| `Attribution` | One claim about one address, with provenance and confidence |
| `Hypothesis` | A scored inference with its factor breakdown |
| `Evidence` | The queries that support a conclusion |
| `Result` | What an analyzer returns: findings + hypotheses + evidence |

### 2.1 `Amount`: why a type, not an int

Forensics with floating point is malpractice. `0.1 + 0.2 != 0.3` stops being an
academic curiosity when you are summing thousands of ETH across a dozen deposits
and reporting the total as evidence.

The raw integer in the asset's smallest unit is the only source of truth.
Decimals exist for display and parsing; they never feed back into arithmetic.
Adding two `Amount`s with different `(decimals, symbol)` raises rather than
producing a plausible wrong number — which matters more than it sounds, because
the same token can carry different decimals on different chains.

A property test caught a real defect here: Python's `Decimal` defaults to 28
significant digits, so a wei-denominated balance above ~10²⁸ silently loses its
low-order digits. Precision is now derived per-operation from the operand size.

### 2.2 `ChainId`: why CAIP-2

A bare `"eth"` is ambiguous across ecosystems — Ethereum, ether, Ethereum
Classic. [CAIP-2](https://chainagnostic.org/CAIPs/caip-2) is an existing
standard shared with WalletConnect and EIP-3770, so anyone can add a chain
without us minting an alias. Short aliases stay available at the CLI boundary
and resolve immediately; they never travel inward.

### 2.3 `Attribution`: provenance in the type system

This is the design decision that matters most.

Blockchain forensics has a recurring failure mode: a heuristic guess is written
down, passes through three tools, and emerges looking like a fact. People get
accused on that basis.

So an `Attribution` cannot be constructed without a `source`, and anything at
`Confidence.LOW` or below cannot be constructed without a `rationale`. The merge
step is non-destructive — conflicting claims are retained, and disagreement
between comparably strong sources is itself surfaced as a finding.

```
CERTAIN   authoritative list, or the contract naming itself on-chain
HIGH      published third-party label (block explorer nametag)
MEDIUM    structural heuristic (co-spend clustering, deposit consolidation)
LOW       behavioural inference (timing, amounts, fee patterns)
SPECULATIVE  a single coincidence
```

Sanctions always take the primary display slot regardless of confidence: it is
the most decision-relevant fact about an address and must never sit beneath a
friendlier label.

---

## 3. Layers

```
┌──────────────────────────────────────────────────────────┐
│ Interface   CLI · Python API · local read API · (MCP)    │
├──────────────────────────────────────────────────────────┤
│ Render      Markdown · JSON · terminal · graph export    │
├──────────────────────────────────────────────────────────┤
│ Analysis    pluggable Analyzers · Watches                │
├──────────────────────────────────────────────────────────┤
│ Attribution Resolver over many Sources, with conflicts   │
├──────────────────────────────────────────────────────────┤
│ Store       normalised entities · graph · rebuildable    │
├──────────────────────────────────────────────────────────┤
│ Chains      Adapters: native format → domain model       │
├──────────────────────────────────────────────────────────┤
│ Providers   capability routing · fallback · breaker      │
├──────────────────────────────────────────────────────────┤
│ Transport   content-addressed cache · TTL · audit        │
└──────────────────────────────────────────────────────────┘
```

Each layer depends only on the one below and on `core`.

`Store` is optional. An analyzer may read straight from `Router` and hold its
working set in memory, which is the right shape for a one-off question. It
becomes necessary the moment a question spans more than one analyzer run — see
§4.8.

---

## 4. Key decisions

### 4.1 Providers declare capabilities, not chains

One chain has many possible data sources with genuinely different powers. A
public RPC endpoint cannot list an address's transaction history at all. An
explorer API can, but cannot trace. An archive provider can answer historical
state queries and bulk asset transfers.

Modelling providers by chain would force every analyzer to know which source can
do what. Instead:

```python
class Capability(Flag):
    BLOCK | TX | RECEIPT | LOGS
    ADDRESS_HISTORY      # explorer-class
    ASSET_TRANSFERS      # bulk native+token+internal in one call
    ARCHIVE_STATE        # historical state at a given block
    TRACE | SOURCE_CODE | UTXO_SET
```

`resolve(chain, capability)` returns candidate providers ordered by user
preference, then cost tier, then health. A failing provider trips a circuit
breaker and is skipped until it recovers.

**The payoff:** a user adds one API key and a capability becomes available on
every chain that provider supports, with no analyzer changes. That is where the
flexibility actually comes from.

### 4.2 Read-only by construction, not by policy

The transport layer has no signing path. `Query` is a closed union of read
operations, and the wire layer additionally rejects `eth_send*`, `eth_sign*`,
`personal_*`, `miner_*`, and `admin_*`.

The check lives at `Client._send`, the one point every outbound request passes
through, and it inspects the request *body* rather than a method argument. That
placement is the whole guarantee. An earlier version checked only inside `rpc()`,
which left two ordinary paths open: a hand-built `post()`, and a JSON-RPC batch,
where the method names sit inside a list and there is no single method argument
to inspect. Explorer APIs need the same treatment, since the Etherscan family
exposes `?module=proxy&action=eth_sendRawTransaction` over a plain `GET`.

The rule generalises: a guarantee enforced at the convenient entry point is a
convention. Enforced where every path converges, it is a property — and it has
to hold for a provider written by someone who never read this document.

A forensics tool that can move funds is a liability to its users. Several
research and competition contexts also mandate read-only analysis outright; a
tool that cannot violate the rule is better than one that merely should not.

### 4.3 Cache TTL derives from finality

Chain history is immutable after finality; chain heads and balances are not.
Treating them identically is a correctness bug, not just a performance one.

```
finalised historical query → cache forever
balance / address stats    → 60s
chain head                 → 5s
```

Keys are content-addressed hashes of the normalised query, so deduplication is
automatic and a cache can be shipped as a reproducible artifact.

### 4.4 Analyzers are plugins

```python
class Analyzer(Protocol):
    name: str
    version: str
    def applicable(self, ctx: Context) -> bool: ...
    async def run(self, ctx: Context, **params) -> Result: ...
```

Registered through entry points. A third-party package that declares
`chainscope.analyzers` appears in `chainscope analyze --list` with no core
change. Same for `chainscope.providers`, `chainscope.chains`, and
`chainscope.attribution_sources`.

**The benchmark this architecture is judged against:** adding a chain, provider,
or attribution source should require one file and one test cassette. If it
requires touching core, the abstraction is wrong.

### 4.5 Analyzers return `Result`; they do not print

```python
@dataclass
class Result:
    analyzer: str
    findings: list[Finding]
    hypotheses: list[Hypothesis]
    evidence: Evidence
    warnings: list[str]
    params: dict          # everything needed to reproduce this run
```

Separating computation from rendering buys four things at once: machine-readable
output, report generation, an audit trail, and testability — you assert on a
`Result` rather than parsing stdout.

### 4.6 Inference is a `Hypothesis`, with its scoring exposed

Cross-chain matching, change-output detection, and clustering are heuristics.
Returning "the answer" from a heuristic is how guesses become facts.

```python
@dataclass(frozen=True)
class Hypothesis:
    claim: str
    score: float
    factors: list[ScoreFactor]      # name, weight, observed value, note
    confidence: Confidence
    alternatives: list[Hypothesis]
```

Factors are individually named and weighted, so a user can see exactly why a
candidate ranked first — and reweight if their context differs.

### 4.7 Case bundles replay offline

```
case.chainscope/
├── manifest.json     tool version, parameters, timestamps
├── queries.jsonl     every request and response, content-addressed
├── results/          each analyzer's Result
└── report.md
```

Three uses: reproducibility (anyone can rerun your analysis without API keys),
offline CI (record once, replay forever — network in CI makes tests flaky and
drives contributors away), and evidence preservation.

Commercial platforms generally cannot offer this, because their underlying data
cannot leave the platform.

### 4.8 The cache and the store answer different questions

These are easy to conflate and expensive to conflate.

The **cache** is keyed by query. It answers *"what did this exact request return
when I made it"*. That is what reproducibility needs, and it is why §4.3 keys on
a hash of the normalised query.

What it cannot answer is *"every address that has ever sent to X"*. No amount of
cached responses gives you that, because the cache has no idea what is inside
them. Yet that is the question every graph traversal, every consolidation
search, and every alert is made of.

So there is a second store, with a different shape:

| | Cache | Store |
|---|---|---|
| Keyed by | hash of the query | entity identity |
| Holds | raw provider responses | normalised `Transfer`, `Address`, `Attribution`, `Cluster` |
| Written by | transport | chain adapters and the attribution resolver |
| Mutability | append-only, immutable | derived, may be rebuilt or discarded |
| Answers | "what did the provider say" | "what is connected to what" |

**The rule that makes this safe: the store must be fully rebuildable from the
cache.** The store is a derived index, never a source of truth. Delete it and
`chainscope rebuild` reconstructs it from recorded responses, offline.

That constraint pays for itself three times:

- A schema change is a rebuild, not a re-crawl. Without it, every change to
  `Transfer` costs everyone their data and a fresh pass over the providers.
- A corrupted or half-written store cannot silently poison an analysis, because
  it can be regenerated and compared.
- It extends §4.7 from a single conclusion to a whole database. Ship a bundle
  and the recipient rebuilds a **query-equivalent** store: the same rows, so
  the same answers. As far as we know nothing else in this space offers that,
  and it follows directly from keeping the two layers separate.

  Not byte-identical, and the difference is worth stating because the stronger
  claim was here and is false. Measured: two stores built from the same
  transfers in the same order hash the same, and the same transfers ingested in
  a different order do not --- rowids follow insertion, and nothing pins the
  order a bundle replays its cached responses in. The property that matters is
  the one the README claims, byte-identical *output*, and that holds because
  analyzers sort. A promise about the file would be a promise about SQLite's
  page layout, which is not what anybody is checking.

It also sets the direction of a rule that is otherwise easy to get backwards:
**the store may be derived from the cache; the cache may never be derived from
the store.** A response that was reconstructed from parsed entities is not
evidence of what the provider said.

Backends are a `Store` protocol with SQLite as the default, because a default
that needs a running server is a default nobody uses. Postgres, DuckDB, and
graph databases are plugins (§4.4). Recursive CTEs over SQLite handle the graph
sizes an address-scoped investigation produces (§4.9); a user who outgrows that
swaps the backend without touching an analyzer.

### 4.9 Ingestion is demand-driven, not full-chain

The graph grows along the path of the investigation. You name a starting address
and a block range; chainscope fetches what that requires, and expands only where
an analyzer or a user actually walks.

This is the decision that determines whether the tool runs on a laptop. The
alternative — index the chain, then query it — is how open-source forensics
projects end up requiring a Spark cluster and a week of backfill before they can
answer anything, at which point the audience narrows to institutions that could
have afforded a commercial license anyway.

The tradeoff is real and worth stating plainly: full-chain indexes can answer
questions demand-driven ingestion cannot, notably unbounded reverse lookups
("every address that ever touched this contract") without an explorer-class
provider to ask. We accept that. Investigations start from a known subject, and
a tool that answers the first thirty addresses well beats one that cannot be
installed.

Consequences that follow, and must be honoured:

- Every traversal is bounded by `Context.limits`, and hitting a bound is a
  `Result.warnings` entry. A graph that stopped because of `max_depth` and a
  graph that stopped because the funds stopped moving look identical otherwise,
  and that ambiguity has produced confident, wrong reports.
- Coverage is a property of a case, not of the tool. A bundle records which
  addresses were expanded and which were seen but never followed, so a reader
  can tell the frontier from the conclusion.

### 4.10 Watches are pure functions; the clock lives outside

Monitoring is a legitimate thing to want and a poor thing to own. A scheduler
means a process, which means uptime, restarts, at-least-once delivery, and a
persistent notion of "now" — and every one of those makes the analysis layer
harder to test and impossible to replay.

So the core provides only the evaluation:

```python
@dataclass(frozen=True)
class Watch:
    name: str
    subject: str            # address, cluster, or saved query
    predicate: Predicate    # "outflow > 10 ETH", "counterparty is sanctioned"
    chain: ChainId

def evaluate(watch: Watch, ctx: Context, since: int, until: int) -> list[Event]
```

`evaluate` is a pure function of a block range. Who calls it is not our concern:
cron, a systemd timer, a CI workflow, a user's own daemon, a `while true` loop.
Freshness becomes the operator's dial, not an architectural constant.

Three properties fall out, and the third is the one that matters:

1. No process to run, no uptime to promise, no clock in the test suite.
2. Delivery is the caller's problem, so we do not have to be wrong about it.
3. **Alerts are replayable.** Evaluation over a fixed block range is
   deterministic, so *"why did this fire?"* is answered by rerunning it against
   the bundle — with the raw responses that triggered it. An alerting system
   that cannot reconstruct its own past decisions is not usable as evidence, and
   most cannot.

An `Event` is a `Finding` with the block range that produced it. Notification
channels — webhook, email, Telegram — are plugins, never core.

### 4.11 The self-host contract

The goal is that someone can stand up their own instance, point it at their own
providers and their own database, extend it for their own domain, and keep
running it without us. That is a different obligation from "the library works",
and it rests on three things that are boring in exactly the way load-bearing
things are.

**Schema stability.** Someone will accumulate a large store and then upgrade.
The store therefore carries a schema version, migrations are shipped with the
release that needs them, and a migration that cannot run must fail loudly rather
than half-apply. §4.8's rebuild guarantee is the escape hatch of last resort,
not the upgrade path.

**Versioned plugin protocols.** `Provider`, `ChainAdapter`, `Analyzer`,
`AttributionSource`, `Store`, and `Renderer` are the surfaces third parties
build against. Each carries an independent protocol version, and the loader
refuses a plugin built against an incompatible one instead of failing later with
an `AttributeError` from inside someone else's code. Stability tiers are
declared per interface and honoured:

| Tier | Promise |
|---|---|
| stable | Breaking change only on a major version, with a migration note |
| provisional | May change on a minor version; changes are in the changelog |
| experimental | May change or vanish at any time; not for downstreams |

**A default that works with nothing configured.** If every capability is a
plugin and nothing ships, a new user's first hour produces no output. There is
one happy path — SQLite store, free public providers, the bundled analyzers,
terminal output — that runs from a clean install with no API key. Plugins exist
to *replace* the defaults, never to *assemble* them.

The stated benchmark from §4.4 extends accordingly: adding a chain, provider,
store backend, or attribution source should be one file and one cassette, and
the person doing it should never need to fork this repository.

---

## 5. Package layout

```
chainscope/
├── core/          models, chain identity, attribution, hypothesis, registry
├── transport/     cache, throttle, audit, http with circuit breaker
├── providers/     base protocol, router, concrete providers
├── chains/        adapters per ecosystem (evm, bitcoin, solana, sui, tron)
├── store/         Store protocol, sqlite backend, rebuild from cache
├── attribution/   sources + conflict-resolving resolver
├── analysis/      one module per technique; see `chainscope analyze --list`
├── osint/         leads: somewhere to look next, kept apart from conclusions
├── watch/         Watch, predicates, evaluate(); no scheduler
├── pricing/       historical rate sources with local cache
├── case/          append-only case log, correspondence ledger, bundles
├── render/        terminal, markdown, json, html, graph and flow export
├── agent/         MCP tools over the same code paths as the CLI
├── server/        loopback-only HTTP for the browser extension
└── cli/           thin dispatch; one module per command group
```

This tree is checked against the source by
`tests/unit/test_the_architecture_doc_describes_this_repo.py`. It had gone
stale in the way a layout diagram always does: `agent/`, `osint/` and `server/`
— three of the surfaces a reader is most likely to be looking for — were
missing entirely, `chains/` did not mention Sui, and `analysis/` named six
modules of the fourteen that exist while listing two (`flow`, `sweep`) that
live elsewhere or not at all. A reader takes a layout as the map; a wrong map
is worse than none.

`analysis/` is deliberately no longer enumerated here. It is the part that grows
fastest, so naming its members in a document guarantees this paragraph is wrong
again by the next release. `chainscope analyze --list` reads the registry.

Domain-specific output formats (e.g. competition submission encoders) live in
separate optional packages, not in core.

---

## 6. Testing

- **Unit** — pure functions, no network
- **Property** (`hypothesis`) — arithmetic laws, normalisation idempotence.
  Already caught the `Decimal` precision defect described in §2.1
- **Cassette** — recorded provider responses in `tests/cassettes/`, replayed
  offline
- **Golden file** — report output compared against snapshots

**CI must never touch the network.** Non-negotiable: flaky tests drive away
contributors faster than missing features do.

---

## 7. Project conventions

| Topic | Choice | Reason |
|---|---|---|
| License | Apache-2.0 | Patent grant; enterprises can adopt it. MIT lacks this |
| Language | English code and docs; translated READMEs | Contributor reach |
| Implementation | Python | Measured, not assumed --- see [docs/why-python.md](docs/why-python.md). The network is 2,451x slower than the local write path, and 74% of that path is already SQLite's C code |
| Datasets | Distributed separately from code | Different licenses; see `docs/data-sources.md` |
| Versioning | SemVer; plugin protocols versioned independently | Plugin compatibility |

### The disclaimer is load-bearing

Heuristic output — clustering, cross-chain matching, change detection — is **not
evidence**. Results below `Confidence.HIGH` must not be used to accuse any person
or entity without independent verification.

This is not liability boilerplate. It is the professional ethic of the field,
and §2.3 exists to enforce it mechanically rather than rely on whoever is reading
the output to remember it.

---

## 8. Roadmap

| Version | Scope |
|---|---|
| v0.1 | core models, transport, provider protocol, EVM + Bitcoin adapters, test harness |
| v0.2 | attribution sources and conflict resolution |
| v0.3 | analyzers: consolidation, cross-chain, peel chains, clustering |
| v0.4 | CLI split, renderers, case bundles |
| v0.5 | `Store` protocol + SQLite backend, `rebuild` from cache (§4.8); explorer-class provider |
| v0.6 | `Watch` + `evaluate()` (§4.10), notification plugins outside core |
| v0.7 | plugin protocol versioning and stability tiers, store migrations (§4.11) |
| v0.8 | plugin authoring docs, Solana/Tron adapters, PyPI release |
| later | local read API, MCP server, graph-database store backend, community label set |

The ordering is deliberate. `Store` precedes `Watch` because an alert that
cannot say what changed since last time is a diff against nothing, and it
precedes plugin versioning because there is no point freezing an interface that
has never had a second implementation.
