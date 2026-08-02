# Extending chainscope

The benchmark this architecture is judged against: **one file and one test
cassette** to add a chain, a provider, an analyzer, or an attribution source.

Everything registers through Python entry points, so your extension can live in
its own repository and its own package. You do not need it merged here.

```toml
# your_package/pyproject.toml
[project.entry-points."chainscope.providers"]
my_indexer = "your_package.provider:MyIndexerProvider"
```

Install alongside chainscope and it appears automatically.

---

## Adding a data provider

A provider answers queries. It declares **what it can do**, not what chain it
is for — the router picks a provider by capability, so one new provider can make
a capability available across every chain it supports without touching any
analyzer.

```python
from chainscope.providers.base import Provider, Capability, CostTier
from chainscope.core.chainid import ChainId, ETHEREUM
from chainscope.transport.cache import Volatility


class MyIndexerProvider(Provider):
    name = "my-indexer"
    chains = frozenset({ETHEREUM})
    capabilities = (
        Capability.ADDRESS_HISTORY
        | Capability.ASSET_TRANSFERS
    )
    cost = CostTier.FREE_KEYED       # needs an API key, but free

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def address_history(self, chain: ChainId, address: str, **kw):
        raw = await self.get(
            f"https://api.example.com/v1/txs/{address}",
            volatility=Volatility.SLOW,
        )
        return [self._to_transfer(x) for x in raw["items"]]
```

**Declare capabilities honestly.** Overstating them is worse than omitting them:
the router will select you, your call will fail or return partial data, and the
analyzer will draw a conclusion from an incomplete picture. A missing capability
degrades gracefully; a lying one produces wrong answers.

**Choose `Volatility` by what the data is**, not how fresh you would like it.
A confirmed transaction is `IMMUTABLE` regardless of when you fetched it.

### Test

```python
def test_address_history(cassette):
    p = MyIndexerProvider(api_key="test")
    with cassette("my_indexer/address_history"):
        got = await p.address_history(ETHEREUM, "0x…")
    assert len(got) == 3
    assert got[0].amount.raw == 1_000_000_000_000_000_000
```

Record the cassette once with `--network`; it replays offline forever after.
Strip API keys from recorded requests before committing — the recorder does this
for known header names, but check.

---

## Adding a chain

Chains are grouped by **ecosystem**, not one adapter each. Every `eip155:*`
network shares one EVM adapter; adding Base or Scroll is a registry entry, not
an adapter.

You only need a new adapter for a genuinely new address format or transaction
model.

```python
from chainscope.chains.base import ChainAdapter
from chainscope.core.chainid import Ecosystem


class MyChainAdapter(ChainAdapter):
    ecosystem = Ecosystem.MYCHAIN

    def normalize_address(self, raw: str) -> str:
        """Canonical form for comparison.

        Be careful here. Lowercasing an EVM address is correct; lowercasing a
        Solana, Tron, or Bitcoin address destroys it, because base58 and bech32
        are case-sensitive. Whatever you return is what equality checks use.
        """
        return raw.strip()

    def parse_transfer(self, raw: dict) -> Transfer:
        ...
```

Adding a network within an existing ecosystem:

```python
from chainscope.core.chainid import ChainId
MY_L2 = ChainId.evm(12345)
```

---

## Adding an analyzer

```python
from chainscope.analysis.base import Analyzer
from chainscope.core.result import Result, Finding


class DormancyAnalyzer(Analyzer):
    """Flag funds that sat still for an unusually long time before moving."""

    name = "dormancy"
    version = "1.0"

    def applicable(self, ctx) -> bool:
        return ctx.chain.ecosystem is Ecosystem.UTXO

    async def run(self, ctx, *, min_days: int = 365) -> Result:
        findings: list[Finding] = []
        ...
        return Result(
            analyzer=self.name,
            findings=findings,
            hypotheses=[],
            evidence=ctx.evidence,
            params={"min_days": min_days},   # everything needed to reproduce
        )
```

**Return a `Result`; never print.** Rendering is a separate layer. This is what
gives you JSON output, Markdown reports, an audit trail, and testable code — all
from one decision.

**Populate `params` completely.** A `Result` that cannot be reproduced from its
own `params` is not evidence, it is an anecdote.

### If your analyzer infers

Anything probabilistic returns `Hypothesis` objects with the scoring exposed:

```python
Hypothesis(
    claim="output 0 is the payment; output 1 is change",
    score=8.5,
    factors=[
        ScoreFactor("recipient_is_fresh", weight=3.0, value=True,
                    note="address has 2 transactions total"),
        ScoreFactor("change_returns_to_input", weight=5.0, value=True,
                    note="output 1 address appears in the input set"),
        ScoreFactor("round_number_payment", weight=-2.0, value=False,
                    note="0.0731 BTC is not a round figure"),
    ],
    confidence=Confidence.MEDIUM,
    alternatives=[...],
)
```

A caller who can see *why* something ranked first can catch your mistake. One
who receives a bare answer cannot — and in this field, catching mistakes is the
whole job.

---

## Adding an attribution source

This one has an extra requirement, and CI enforces it.

```python
from chainscope.attribution.base import Source
from chainscope.core.attribution import (
    Attribution, Category, Confidence, Method,
)


class MyListSource(Source):
    name = "my-list"

    async def lookup(self, address: str, chain=None) -> list[Attribution]:
        hit = self._index.get(address.lower())
        if not hit:
            return []
        return [
            Attribution(
                address=address,
                chain=chain,
                label=hit["name"],
                category=Category.CEX,
                confidence=Confidence.HIGH,   # published label
                method=Method.LABEL,
                source=f"my-list@{self.snapshot_date}",
                observed_at=self.snapshot_date,
            )
        ]
```

**Version your `source` string.** `"my-list@2026-08-01"` lets an analyst tell
which snapshot produced a claim when the upstream data later changes. A bare
`"my-list"` cannot.

**Return a list.** An address can legitimately carry several claims; do not pick
one for the caller. The resolver merges them non-destructively, and disagreement
between sources is itself a finding.

### The mandatory part

Add a row to [`docs/data-sources.md`](data-sources.md) **in the same pull
request**, covering:

- Publisher
- License
- Whether redistribution is permitted
- The confidence level it maps to, and why

CI fails the build if a source module has no matching entry. This is deliberate:
a source whose provenance nobody wrote down becomes, three tools later, a fact
nobody can trace back to anything.

---

## Adding a renderer

```python
from chainscope.render.base import Renderer

class GraphvizRenderer(Renderer):
    name = "dot"
    def render(self, result: Result) -> str:
        ...
```

Renderers must show confidence. Anything below `Confidence.HIGH` renders as a
claim, with its rationale visible — not as a plain label. If your renderer drops
that distinction to look tidier, it will not be merged; that distinction is the
product.

---

## Testing your extension

```python
# tests/conftest.py in your package
pytest_plugins = ["chainscope.testing.fixtures"]
```

Provides `cassette`, a pre-populated `Cache`, and factory helpers.

Run with sockets blocked (the default). Your extension's tests should pass on a
machine with no network and no API keys — if they cannot, the cassette is
incomplete.

---

## Getting it listed

Extensions that follow these conventions can be listed in the README. Open an
issue with a link, a one-line description, and confirmation that the tests pass
offline.

We will not vendor your code, and you keep your own release cadence.
