# Data sources and licensing

chainscope ships **code**, not data. Attribution datasets are fetched at install
or run time from their upstream sources, because their licenses differ from
Apache-2.0 and in several cases forbid redistribution.

This page exists so you can check, before you rely on it in a report, where a
label came from and whether you are allowed to use it that way.

---

## Shipped source adapters

Each module under `src/chainscope/attribution/sources/` must appear here. CI
fails the build otherwise --- see [CONTRIBUTING](../CONTRIBUTING.md).

| Module | Adapter | Confidence ceiling | Redistributable |
|---|---|---|---|
| `ofac` | `OfacSource` | `CERTAIN` | Yes --- US government work |
| `etherscan_dump` | `ExplorerDumpSource` | `HIGH` | No --- upstream terms apply |
| `local` | `LocalSource` | `MEDIUM` | N/A --- your own data |

The ceilings are enforced in code (`SourceMeta.max_confidence`), not merely
documented. A community nametag dump cannot assert `CERTAIN` even if its adapter
passes that value in.

---

## Rule of thumb

| If the label came from… | You may… | You must… |
|---|---|---|
| A government sanctions list | Use and redistribute freely | Cite the list and its date |
| A block explorer's public nametags | Use for research | Check that explorer's ToS before redistributing |
| A community dataset | Follow its stated license | Preserve attribution |
| chainscope's own heuristics | Treat as a lead, not a finding | State the method and confidence |

---

## Sanctions lists

### OFAC Specially Designated Nationals (SDN)

- **Publisher:** U.S. Department of the Treasury, Office of Foreign Assets Control
- **Canonical:** <https://sanctionslist.ofac.treas.gov/Home/SdnList>
- **Status:** U.S. government work — public domain
- **Redistribution:** permitted
- **Confidence assigned:** `CERTAIN`, `Method.LIST`

Machine-readable digital-currency address extracts are available from community
mirrors. Mirrors are convenient but **not authoritative**: before an address's
sanctioned status affects a real decision, verify against the official list. The
source string records which mirror and which snapshot date was used.

### Other jurisdictions

EU, UK OFSI, and UN consolidated lists publish sanctioned crypto addresses less
consistently. Not yet integrated; contributions welcome, and each needs its own
entry here before merge.

---

## Block explorer labels

### Etherscan and family

- **Nature:** nametags curated and published by the explorer
- **Confidence assigned:** `HIGH`, `Method.LABEL`
- **Coverage:** strong for large exchanges, DeFi protocols, and bridges

**Two limitations worth internalising:**

1. **Coverage is uneven and lags.** Smaller or newer services are frequently
   absent. In practice this is the single biggest gap in public-data forensics —
   an unlabelled address is not an unknown entity, it is an entity nobody has
   published a label for yet.
2. **Bitcoin has no equivalent.** There is no public nametag database for UTXO
   chains at all. Attribution there rests almost entirely on clustering
   heuristics and behavioural inference, which is why those get `MEDIUM` and
   `LOW` confidence respectively.

**Redistribution:** community dumps of explorer labels circulate on GitHub.
Check the specific repository's license and the explorer's terms before
redistributing. chainscope fetches such dumps at run time and does not vendor
them.

---

## Community abuse reports

### Chainabuse and similar

- **Nature:** user-submitted scam and abuse reports
- **Confidence assigned:** `MEDIUM` at best; `LOW` for single unverified reports
- **Caution:** user-submitted data is unverified by construction, and reports
  can be malicious. Never treat a single report as attribution.

---

## Prices

Historical exchange rates come from public market-data APIs (Binance klines by
default — free, no key required). Rates are cached locally.

**Note that a spot rate is not the rate a service actually gave.** Services quote
with a spread and a fee. chainscope's cross-chain matcher calibrates the
effective discount from confirmed cases rather than assuming spot, and reports
the calibrated figure alongside the spot figure.

---

## chainscope's own inferences

Anything the tool derives itself carries `Method.HEURISTIC` or
`Method.INFERENCE` and is capped at `MEDIUM` and `LOW` confidence respectively.

The methods are documented, not proprietary:

| Method | Confidence | Reference |
|---|---|---|
| Common-input-ownership clustering | `MEDIUM` | Meiklejohn et al., *A Fistful of Bitcoins* (2013) |
| Deposit-address consolidation | `MEDIUM` | `docs/methods/consolidation.md` |
| Change-output detection | `MEDIUM` | `docs/methods/change-detection.md` |
| Cross-chain time/amount matching | `LOW` | `docs/methods/cross-chain.md` |

Each documents its assumptions **and the conditions under which it fails** —
CoinJoin defeats co-spend clustering, exchange batching defeats consolidation
analysis, and any of them can be defeated deliberately by an adversary who knows
they are being watched.

---

## Adding a source

A new attribution source needs, in the same pull request:

1. The adapter (one file under `attribution/sources/`)
2. A test cassette
3. **A row in this document** covering: publisher, license, redistribution
   terms, and the confidence level it maps to

The third item is not paperwork. A source whose provenance nobody wrote down
becomes, three tools later, a fact that nobody can trace back.
