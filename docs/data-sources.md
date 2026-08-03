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
| `darklist` | `DarklistSource` | `MEDIUM` | Yes --- MIT |
| `eth_labels` | `EthLabelsSource` | `MEDIUM` | **No** --- see below |
| `contracts_list` | `ContractsListSource` | `MEDIUM` | **No** --- no licence declared |

The ceilings are enforced in code (`SourceMeta.max_confidence`), not merely
documented. A community nametag dump cannot assert `CERTAIN` even if its adapter
passes that value in.

## `darklist` --- community scam reports

- **Canonical:** <https://github.com/MyEtherWallet/ethereum-lists>
- **Fetch:** `src/addresses/addresses-darklist.json` from `raw.githubusercontent.com`
- **Licence:** MIT. Redistributable, and the URL is one a reader can open to
  check any claim made from it.
- **Ceiling:** `MEDIUM`. Entries are submitted by people who were defrauded or
  who investigated a fraud and reviewed by maintainers. That is real evidence
  and it is not the published legal fact that lets `ofac` say `CERTAIN`.
- **Coverage:** ~715 Ethereum addresses. A rounding error against the number
  that exist, and skewed heavily towards one era's phishing campaigns.
  **Absence is not a clean result**, and the adapter raises rather than
  returning an unqualified empty list when asked about a non-Ethereum chain.
- **Staleness is one-directional.** The list does not un-report, so a hit is a
  statement about the past. Each entry's date travels with the attribution.

## `contracts_list` --- named contracts, with their own provenance

- **Canonical:** <https://github.com/ethereum-lists/contracts>
- **Fetch:** `contracts_list.clone()` --- a depth-1 `git clone`, ~45 MB.
  **252,268** contract files across many chains as of August 2026.
- **Why cloned and not crawled:** one JSON file per contract. Sixty thousand
  HTTP requests to build a label table is not a thing to do to GitHub or to
  somebody's rate limit.
- **Licence: none declared.** GitHub reports no licence, so the default applies
  and there is no permission to redistribute. `redistributable` is `False` and
  the data is never bundled --- shipping it would be a licence violation dressed
  as convenience.
- **Ceiling:** `MEDIUM`. Each record carries a `source` field naming where the
  name came from (`dune`, and others), which is more provenance than any other
  open dump here offers --- and it is still somebody else's judgement recorded
  without evidence.
- **A contract name is not an entity attribution.** "This contract belongs to
  the topbidder project" is much narrower than "this address is controlled by
  X", so entries are `CONTRACT`. Reading a deployment record as an ownership
  claim is the error that distinction prevents.
- **Chain is required.** The registry is keyed by chain reference and the same
  twenty bytes are a different contract on each; a chainless lookup would
  attribute one deployment's name to another.

## `eth_labels` --- the largest open address dump

- **Canonical:** <https://github.com/dawsbot/eth-labels>
- **Fetch:** `chainscope.attribution.sources.ethlabels.fetch()`, or the four
  `src/mainnet/<category>/all.json` files from `raw.githubusercontent.com`
- **Coverage:** 17,495 mainnet addresses as of August 2026 --- 11,517
  token contracts, 5,594 `phish-hack`, 372 exchanges, 12 genesis.
- **Ceiling:** `MEDIUM`, and the reason is provenance rather than volume. The
  dump is **Etherscan's label data, reorganised**. Etherscan does not publish it
  under a licence permitting that, so an entry is somebody's transcription of
  somebody else's judgement, with no evidence attached and no way to ask who
  decided or when.
- **Redistributable: no.** The MIT licence covers the repository's arrangement
  of the data, not the data. `SourceMeta.redistributable` is `False` so nothing
  here invites shipping it.
- **`phish-hack` is carried as `ILLICIT`, not `SCAM`.** The upstream bucket
  mixes thefts ("Bancor Hacker") with frauds ("Fake_Phishing"), and collapsing
  them into the narrower word would state something the data does not support.
  A name like that in a report is an accusation; the rationale says the claim
  rests on a community list rather than on anything this tool verified.

### Why this one and not the others

The well-known recommendation lists name several scam databases. Checked, in
August 2026:

| Source | Result |
|---|---|
| CryptoScamDB API | `502` — the service is down |
| Chainabuse API | `401` — needs a key |
| Etherscan label export | `403` to anything that is not a browser |
| Blockscout `public_tags` | In the schema, empty in practice; no cross-instance dump |
| Dune `labels.addresses` | Needs an API key; not anonymously downloadable |
| WalletLabels | Commercial API, no open dataset |
| OpenChain | Reachable, but it maps **function selectors**, not addresses |
| `ethereum-lists/contracts` | **Added** --- see above. 252,268 files, no declared licence |
| L2Beat / DefiLlama | Alive, but protocol registries rather than address attribution |

Being on a recommendation list and being obtainable are different properties,
and the gap is wider than the lists suggest.

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
