# Cross-chain matching

**Confidence produced:** `LOW` — capped, never higher. It is the only
confidence the module names.
**Implemented by:** `chainscope.analysis.xchain.CrossChainMatcher`

> **On this header.** It used to name a `Confidence` *and* a `Method`. The
> method was never true of any of these four documents: `Method` describes how
> an `Attribution` was arrived at, and none of these analyzers writes one. They
> emit findings and hypotheses. `tests/unit/test_method_docs_match_the_code.py`
> now checks each claim below against the module it names, so a header that
> stops being true fails the suite rather than misleading a reader.


---

## The problem

Funds enter a swap service on one chain and leave on another. Nothing on either
chain links the two sides: no shared address, no reference field, no receipt.
The chains do not know about each other.

What remains is correspondence. A deposit at time *T* produces a payout shortly
after, of an amount that matches at the rate then prevailing.

This is the one analysis in the toolkit that is genuinely infeasible by hand —
it means examining every output in a block range — and it is also the one most
likely to produce a confident-looking wrong answer.

## The three dimensions

### Time — usually the strongest constraint

A service that settles within 5–45 minutes narrows Bitcoin to two or three
blocks. That single constraint cuts the candidate set by orders of magnitude
before amount is even considered. Apply it first.

### Amount — but not the spot-equivalent

Services quote with a spread and take a fee, so the payout sits consistently
*below* spot. Searching at spot finds nothing. The instinctive fix — widen the
tolerance — floods the results with unrelated transactions and destroys the
discrimination that time bought you.

**Calibrate instead.** Take swaps you have already confirmed, measure the
effective discount, and search around that:

```python
cal = calibrate(
    [(Decimal(250),  "ETH", t1, Decimal("9.7050")),
     (Decimal(1000), "ETH", t2, Decimal("38.8200")),
     (Decimal(1008), "ETH", t3, Decimal("39.1300"))],
    "BTC", prices,
)
cal.mean_discount   # ~2.93%
cal.spread          # ~0.06 percentage points
cal.is_consistent   # True
```

The spread is the number that matters. **Independent swaps settling within about
0.2 percentage points of each other is very hard to explain by coincidence** —
it is stronger evidence that one service handled all of them than any single
amount match, and it is what makes the technique work at all.

### Payer — the tiebreaker

Once a handful of candidates remain, the paying wallet separates them:

| Signal | Weight | Reasoning |
|---|---|---|
| Payer has ≥10,000 transactions | +5 | Service hot wallet |
| Payer has ≤3 transactions | −3 | A one-off address is not a service |
| Recipient has ≤2 transactions | +3 | Payouts land on fresh addresses |
| Exactly two outputs | +1.5 | Payment plus change |
| Discount within 0–5% | +2 | Plausible fee structure |

Weights are exposed on every `Hypothesis`, so a reviewer who disagrees can see
which factor to argue with rather than having to accept or reject the whole
answer.

## When this fails

### It produces false positives

- **Coincidence.** Two unrelated people transacting similar amounts minutes
  apart is not rare at scale. This is the dominant failure mode, and the reason
  confidence is capped at `LOW`.
- **Batched payouts.** A service that pays several customers in one transaction
  breaks the one-deposit-one-payout assumption entirely.
- **Near ties.** When the top two candidates score within 1.0 of each other, the
  ranking is arbitrary. The analyzer sets `is_contested` and adds a warning;
  reporting the winner anyway is reporting a coin flip.

### It produces false negatives

- **The swap targeted a different asset.** An empty result usually means the
  deposit bought XMR, not BTC. It does not mean nothing happened.
- **Settlement outside the window.** Congestion, manual review, and compliance
  holds all push payouts past a nominal service-level window.
- **Fees outside the searched band.** Large orders sometimes get better rates;
  small ones get worse.
- **Triangulated rates.** When no direct pair exists, the rate goes through
  USDT and carries two spreads. The analyzer records this and warns; widen the
  band accordingly.

### It can be defeated deliberately

Splitting one deposit across several payouts, adding a random delay, or routing
through a service that batches all defeat this cleanly. Anyone who knows they
are being traced can do all three.

## Interpreting the output

| Situation | What you may say |
|---|---|
| Clear winner, calibrated band, consistent discount | "A payout consistent with this deposit appears at *T+n*" |
| Clear winner, uncalibrated | "A candidate exists" — and go calibrate |
| `is_contested` is true | Report nothing; name it as an open question |
| No candidates | "No BTC payout matched" — *not* "the funds stopped" |

Never write "the funds went to address X" on the strength of this alone. The
correct sentence names the correspondence and its basis: amount, timing, and
the payer's characteristics.

## Cost

Scanning two Bitcoin blocks is roughly 5,000 transactions. With a warm cache
that is seconds; cold, it is a few minutes of API calls. Prefetch rates for the
case window first — `BinanceKlines.prefetch` — so the analysis itself runs
offline and can be replayed later.
