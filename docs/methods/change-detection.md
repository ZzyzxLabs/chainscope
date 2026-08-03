# Change detection

**Confidence produced:** `MEDIUM`, and `SPECULATIVE` where there is nothing to
go on — the module carries both, and the weaker one is not a footnote.
**Implemented by:** `chainscope.analysis.peel.detect_change`

> **On this header.** It used to name a `Confidence` *and* a `Method`. The
> method was never true of any of these four documents: `Method` describes how
> an `Attribution` was arrived at, and none of these analyzers writes one. They
> emit findings and hypotheses. `tests/unit/test_method_docs_match_the_code.py`
> now checks each claim below against the module it names, so a header that
> stops being true fails the suite rather than misleading a reader.


---

## The problem

A UTXO transaction with two outputs pays someone and returns the remainder to
the spender. Which is which is not recorded anywhere — the chain treats both
identically.

Following a chain of spends means making that call at every hop. Get it backwards
once and you follow the payment into a dead end while the funds walk away, and
the resulting trail looks exactly as authoritative as a correct one.

## Heuristics, strongest first

| Signal | Weight | Reasoning |
|---|---|---|
| Output pays back into the input set | +5 | Address reuse; near-conclusive when present |
| Recipient address is brand new | −3 | Payees get fresh addresses; change returns to the wallet |
| Amount is a round number | −2 | Humans choose payment amounts; wallets do not choose change |
| Script type matches the inputs | +2 | Wallets generate change of their own type |
| Strictly the largest output | +2 | Peel-chain change carries most of the value |

The last one only counts when one output is *strictly* largest. With two equal
outputs, picking "the first largest" would hand one of them a decisive edge
invented from nothing — which is the arbitrary tiebreak this whole module exists
to refuse.

## When it fails

- **CoinJoin.** Equal outputs by construction, no change in the ordinary sense.
  The heuristics have nothing to work with and will still produce a ranking;
  check `is_contested`.
- **Consolidation.** Many inputs, one output. Nothing to decide, and nothing to
  follow.
- **Round-number change.** A wallet sweeping exactly 1.0 BTC to itself inverts
  the round-number signal.
- **Address reuse by the payee.** If the recipient reuses an address that also
  appears in the inputs — rare but possible in exchange flows — the strongest
  heuristic points the wrong way.
- **Deliberate mimicry.** Any of these can be inverted on purpose by someone who
  knows they are watched.

## Interpretation

`ChangeDecision.confident` is false when the top two candidates score within
1.0 of each other. `PeelChainAnalyzer` stops walking at that point by default,
because everything downstream of an ambiguous hop rests on a coin flip.

Never report a peel chain without stating where, if anywhere, the change
decision was contested.
