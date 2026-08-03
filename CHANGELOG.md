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
- **Chains** — EVM, Bitcoin, Solana, Sui, Tron adapters
- **Attribution** — OFAC sanctions, explorer nametag dumps, local labels, ENS
  with forward confirmation; resolver that distinguishes "nothing known" from
  "could not check", and bulk import that checks the address column
- **Analysis** — fourteen techniques, each a module under `chainscope/analysis`
  and each reachable through `chainscope analyze --list`. Not enumerated here:
  the list grows, and a list in a changelog entry is a promise to be wrong
- **OSINT** — leads, which are explicitly not attributions: every one names the
  step that would confirm it, and that step is always something a person does
  somewhere this tool cannot reach
- **Case record** — an append-only log where a correction must name what it
  supersedes, and a correspondence ledger where overdue is derived rather than
  stored and silence is never recorded as a refusal
- **Pricing** — Binance minute klines with a local cache, and `value`, which
  converts at the rate that applied when the money moved and refuses when the
  nearest rate is too far away to defend
- **Render** — terminal, Markdown, JSON, HTML dashboard, and an interactive
  flow graph with per-analyst annotations
- **Case bundles** — results plus the recorded responses that produced them,
  replayable offline
- **CLI** — sixteen commands; `chainscope --help` lists them
- **MCP agent** — twelve tools, including writes, over the same code paths as
  the CLI rather than a parallel implementation
- **Browser extension** — MV3, talking to a loopback-only local server

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
- `.lower()` was called on addresses in 27 places, bypassing the one function
  that knows base58 and bech32 are case-sensitive. Measured cost: zero-row store
  queries on three of five chains, a permanently stuck expansion frontier, every
  revenue share computed as 0 bps, and sanctions lookups that missed listed
  addresses. The transmission mechanism was a worked example in
  `docs/extending.md`, which is why the count reached 27
- The flow page's JavaScript had never parsed. Every test passed throughout,
  because each extracted one function with a regular expression first — a
  fragment that parses says nothing about the file it came from
- Three providers declared `Capability.TRANSACTION` and raised
  `NotImplementedError` when asked, so the router preferred them and the call
  failed
- `block_at_time` returned a block *after* the moment asked about — the exact
  failure its docstring says it exists to prevent — because the search was
  seeded with the lowest block. It also excluded the genesis block, and treated
  a transient read failure as evidence that a block was too late
- The attribution resolver cached resolutions in which a source had failed, so
  one rate limit was remembered for the rest of the run and every later lookup
  of that address returned the same partial answer
- Bulk label import validated the label and the category and never the address,
  so a column mapping off by one imported a whole file of names as addresses and
  reported them as ready
- The dashboard's summary table dropped `decimals` and assumed 18, showing 1,000
  USDC as `0.000000` — beside a flows table that read the same rows correctly.
  Both formatters also cut the fraction at a fixed position, so one wei rendered
  as zero, which on a flow graph reads as nothing having moved
- The network guard that keeps CI offline was installed by a function-scoped
  fixture, so collection-time code, session- and module-scoped fixtures, and any
  `from socket import …` binding went straight out
- All four method documents named a `Method` that appeared nowhere in the code
  they pointed at, and `ARCHITECTURE.md`'s package layout omitted three whole
  packages. Both are now checked by tests, because documentation drifts exactly
  as far as nothing stops it
