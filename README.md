# chainscope

**Open-source blockchain forensics — everything you can do with public data.**

[繁體中文](README.zh-TW.md)

> ⚠️ **Alpha.** APIs will change. See [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

---

## Why

Commercial blockchain analytics (Chainalysis, Elliptic, TRM) rest on a moat we
cannot and should not try to replicate: a decade of proprietary attribution data
built from subpoenas, exchange partnerships, and undercover purchases.

But three of their four core capabilities need no proprietary data at all:

| Capability | Reproducible from public data? |
|---|---|
| Cross-chain matching | ✅ It is a search problem: time × amount × behaviour |
| Clustering & consolidation inference | ✅ Published algorithms |
| Fund-flow tracing | ✅ Graph traversal |
| Entity attribution | ⚠️ Only from public labels plus documented heuristics |

chainscope implements the reproducible parts, honestly, for the people who do
not have a six-figure license: journalists, researchers, small enforcement
teams, hacked protocols, and CTF players.

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

## Install

```bash
pip install chainscope            # core
pip install "chainscope[all]"     # + EVM, Bitcoin, Solana, rich output
```

## Status

v0.1 is being built. Implemented so far:

- [x] `core.units` — exact `Amount` arithmetic, no floats, ever
- [x] `core.chainid` — CAIP-2 chain identity
- [x] `core.attribution` — provenance, confidence, non-destructive merge
- [x] `transport` — content-addressed cache, finality-derived TTL, throttling, audit log
- [x] `providers` — capability-routed data sources with fallback
- [x] `chains` — EVM and Bitcoin adapters
- [x] `attribution` — OFAC, explorer nametags, local labels, conflict resolution
- [x] `analysis` — consolidation, cross-chain matching, peel chains, clustering
- [ ] `cli` and renderers

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and roadmap.

## Extending

Adding a chain, a data provider, or an attribution source should take **one file
and one test cassette**. That is the benchmark this architecture is measured
against. Register via entry points; no core changes:

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
