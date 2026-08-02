# Changelog

Notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

During `0.x`, minor versions may break APIs. The plugin protocols
(`Provider`, `Analyzer`, `Source`, `Renderer`) are versioned independently and
changes to them are called out here explicitly.

## [0.1.0] — unreleased

First release. The whole surface is new, so rather than list every module, here
is what the release actually commits to.

### The design commitment

An `Attribution` cannot be constructed without a `source`, and a claim at
`Confidence.LOW` or below cannot be constructed without a `rationale`. Merging
is non-destructive, sanctions never sit beneath a friendlier label, and every
renderer hedges weak claims in plain words.

This is the point of the project: a heuristic guess should not be able to pass
through three tools and emerge looking like a fact.

### Added

- **Core** — `Amount` (exact integer arithmetic, no floats), CAIP-2 `ChainId`,
  `Attribution` with provenance and confidence, `Hypothesis` with exposed score
  factors, `Result`/`Finding`/`Evidence`
- **Transport** — content-addressed cache with finality-derived TTLs, per-host
  token-bucket throttling, credential-redacting audit log, circuit breaker
- **Providers** — capability-based routing with cost-tier preference and
  fallback; generic EVM JSON-RPC provider
- **Chains** — EVM, Bitcoin, Solana, Tron adapters
- **Attribution** — OFAC sanctions, explorer nametag dumps, local labels;
  resolver that distinguishes "nothing known" from "could not check"
- **Analysis** — deposit-address consolidation, cross-chain matching, peel
  chains, co-spend clustering
- **Pricing** — Binance minute klines with a local cache
- **Render** — terminal, Markdown, JSON
- **Case bundles** — results plus the recorded responses that produced them,
  replayable offline
- **CLI** — `analyze`, `label`, `doctor`, `bundle`

### Security properties

- Read-only by construction. The transport layer rejects `eth_send*`,
  `eth_sign*`, `personal_*`, `miner_*`, and `admin_*` before a request is built.
  Enforced at `Client._send` — the single point every outbound request passes
  through — by inspecting the request body, so JSON-RPC batches, hand-built
  `post()` calls, and explorer `?action=eth_sendRawTransaction` over `GET` are
  covered, not only `rpc()`
- Case bundles are treated as untrusted input: JSON only, no pickle, no dynamic
  import, manifest version checked
- API keys are redacted from the audit log, including keys embedded in URL paths

### Notes on defects found during development

Recorded because each would have reached a user, and because they are the kind
of failure this project is specifically meant to avoid.

- `Decimal` defaults to 28 significant digits, so wei balances above ~10²⁸
  silently lost their low-order digits. Caught by a property test; precision is
  now derived per operation
- `chainscope[evm]` installed cleanly and then failed at the first hash, because
  `eth-utils` delegates keccak to `eth-hash`, which ships no backend
- `Throttle.acquire` deadlocked under concurrency by taking a non-reentrant lock
  twice; single-threaded tests never noticed
- JSON-RPC responses were cached under a key derived from the endpoint *host*,
  so `host/eth` and `host/bsc` collided and the second chain silently received
  the first chain's answer. Tracing one contract deployed at the same address on
  several networks walks straight into it. Keyed on chain identity now, which
  also makes a recorded cache replayable against a different node
- The read-only guarantee was enforced inside `rpc()` only, leaving `post()` and
  JSON-RPC batches unchecked. Moved to `Client._send` and made to inspect the
  request body; the regression test shows the pre-fix code putting a broadcast
  on the wire
- Peel-chain change detection awarded a decisive "largest output" bonus to
  whichever of two equal outputs came first — an arbitrary tiebreak in the one
  place the analyzer exists to refuse to make one
- Clustering stopped queueing at `max_addresses` without setting its truncation
  flag, so the walk ended with an empty queue and looked complete
- `ResolvedEntity.disputed` fired on every sanctioned exchange, because
  SANCTIONED was compared against service categories rather than treated as the
  overlay it is
