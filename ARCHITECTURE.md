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
- Real-time monitoring and alerting (a different system with different
  latency/durability tradeoffs; would distort this design)
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
│ Interface   CLI · Python API · (later) HTTP / MCP        │
├──────────────────────────────────────────────────────────┤
│ Render      Markdown · JSON · terminal · graph           │
├──────────────────────────────────────────────────────────┤
│ Analysis    pluggable Analyzers                          │
├──────────────────────────────────────────────────────────┤
│ Attribution Resolver over many Sources, with conflicts   │
├──────────────────────────────────────────────────────────┤
│ Chains      Adapters: native format → domain model       │
├──────────────────────────────────────────────────────────┤
│ Providers   capability routing · fallback · breaker      │
├──────────────────────────────────────────────────────────┤
│ Transport   content-addressed cache · TTL · audit        │
└──────────────────────────────────────────────────────────┘
```

Each layer depends only on the one below and on `core`.

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

---

## 5. Package layout

```
chainscope/
├── core/          models, chain identity, attribution, hypothesis, registry
├── transport/     cache, throttle, audit, http with circuit breaker
├── providers/     base protocol, router, concrete providers
├── chains/        adapters per ecosystem (evm, bitcoin, solana, tron)
├── attribution/   sources + conflict-resolving resolver
├── analysis/      consolidation, xchain, peel, cluster, flow, sweep
├── pricing/       historical rate sources with local cache
├── render/        terminal, markdown, json, graph
└── cli/           thin dispatch; one module per command group
```

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
| v0.5 | plugin authoring docs, Solana/Tron adapters, PyPI release |
| later | HTTP API, MCP server, graph-database backend for large cases |
