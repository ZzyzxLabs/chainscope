# Common-input-ownership clustering

**Confidence produced:** `MEDIUM` (`Method.HEURISTIC`)
**Implemented by:** `chainscope.analysis.cluster.CoSpendClusterAnalyzer`

---

## The technique

To spend several inputs in one transaction you must produce a signature for each,
so those addresses are controlled by one party. Applied transitively, this
expands a single address into a wallet.

It is the oldest heuristic in Bitcoin forensics and the foundation of every
commercial clustering product. It is also usually the highest-value single step
in a UTXO investigation: it turns "this address received 2 BTC" into "this
entity controls 340 BTC across 89 addresses".

## The two things that go wrong

### CoinJoin inverts the assumption

In a collaborative transaction the inputs belong to *different* people by
design. Clustering through one does not merely add noise — it merges unrelated
parties, and because expansion is transitive, a single CoinJoin can poison the
entire result.

Detection is therefore mandatory rather than optional here:

```python
looks_like_coinjoin(input_count, output_values, threshold=5)
```

Five or more equal-valued outputs alongside five or more inputs. The threshold
errs toward suspicion on purpose: a false positive costs one skipped
transaction, a false negative merges strangers into your subject's wallet.

It catches the equal-output signature of Wasabi, JoinMarket, and Whirlpool. It
does **not** catch PayJoin, coin control that deliberately avoids co-spending, or
custom collaborative protocols.

### A cluster is not an identity

Clustering shows that addresses share a controller. It does not say who, and it
cannot distinguish an individual from a custodian holding funds for a hundred
thousand users.

This matters practically: a cluster of tens of thousands of addresses is an
exchange, and walking into it *ends* a trace rather than advancing it. The
analyzer raises an `IMPORTANT` finding when a cluster exceeds 10,000 addresses
for exactly that reason.

## Limits and truncation

`max_addresses` and `max_transactions` bound the expansion. When either bites,
the result is flagged `truncated` and a warning says so.

Read it. A truncated cluster is a **lower bound** on the wallet — describing it
as "the wallet" understates holdings, sometimes by orders of magnitude.

## What you may say

| Situation | Claim |
|---|---|
| Clean expansion, no CoinJoins skipped | "These N addresses share a controller" |
| CoinJoins were skipped | Same, plus: coverage is incomplete by design |
| Truncated | "At least N addresses" — never "N addresses" |
| Cluster > 10,000 | "Consistent with custodial infrastructure" — a lead, not a finding; never attribute to an individual |

Never write "X owns these addresses" from clustering alone. The supportable
sentence is about shared control, not identity.

And the last row is a *lead*. Size is consistent with an exchange and also with
a widely-used contract, a CoinJoin coordinator, or a heuristic that over-merged
--- the same over-merge this page spends its length warning about. "A custodial
service" is a category, stated flatly, from a count; the analyzer raises
`IMPORTANT` there so somebody looks, not so somebody concludes.

## References

- Meiklejohn, Pomarole, Jordan, Levchenko, McCoy, Voelker, Savage,
  *A Fistful of Bitcoins: Characterizing Payments Among Men with No Names*,
  IMC 2013 — the paper that established this heuristic and its limits.
