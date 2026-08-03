# Handing a case to somebody else

A case is not one file, and which of them travel is a decision about what you
are asserting rather than a packaging detail.

## What a case is made of

| File | What it holds | Rebuildable? |
|---|---|---|
| `.chainscope/store.db` | transfers, addresses, attributions | **Yes** — from the cache |
| `.chainscope/case.db` | notes, leads, correspondence | **No** — it is somebody's work |
| `.chainscope/http.sqlite` | the recorded provider responses | No — but re-fetchable |
| `data/labels/` | public datasets | Yes — `chainscope labels fetch` |
| `.env` | your API keys | **Never send this** |

The split is deliberate. `store.db` is *derived*: throw it away and
`chainscope bundle` rebuilds it from the cache. `case.db` cannot be recomputed
from chain data because it did not come from chain data — it is what somebody
noticed, decided, and asked.

## Three ways to hand it over, and what each claims

### The evidence, replayable — `chainscope bundle export`

Results plus the recorded responses that produced them. The recipient can
re-run the analysis offline and get the same answer, which is the strongest
thing you can hand over: they are not taking your word for the data.

Use this when the conclusion matters and somebody has to check it.

### The reasoning too — add `case.db`

Your notes, your open questions, your leads and what came of them. Superseded
notes are kept and struck through, so "I thought X, then found Y" travels with
it — which is usually the most useful part and the part people are most tempted
to tidy away.

Use this when somebody is taking the case over rather than reviewing it.

### A view — the `share` button in `chainscope serve`

A URL that restores the seed, chain, hidden assets and watermark. It carries
**no data**: the case lives in your store and is not the link's to send. The
page says so when you copy it.

Use this when you are both looking at the same case already.

## What must not travel

**`.env`.** It is gitignored and `bundle` does not read it, but a directory
copied wholesale will include it. Check before you send.

**The label datasets, if you are redistributing.** `contracts_list` declares no
licence and `eth_labels` repackages Etherscan's data — the recipient should run
`chainscope labels fetch` rather than receive a copy. `chainscope labels` prints
each one's terms.

## On the other side

```bash
chainscope bundle import case.zip
chainscope labels fetch          # their own copy, under their own terms
chainscope serve
```

`bundle import` verifies the manifest version and refuses anything it does not
understand rather than importing part of it. A bundle is untrusted input: JSON
only, no pickle, no dynamic import.
