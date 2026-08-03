# Deposit-address consolidation

**Implemented by:** `chainscope.analysis.consolidation.ConsolidationAnalyzer`

**Confidence produced:** none of its own.

This header used to read *"Confidence produced: `MEDIUM` (`Method.HEURISTIC`)"*.
It was not true: the analyzer writes no `Attribution` at all, so it produces no
confidence and no method. What it emits is findings, and the only confidence in
them belonged to the *hub's existing label* — under the field name `confidence`,
on a cluster object, where it read as confidence in the cluster. That field is
now `label_confidence`, and every finding carries a `clustering` note saying
what the structure alone supports.

> **On this header.** All four method documents named a `Method` that was never
> true of them: `Method` describes how an `Attribution` was arrived at, and none
> of these analyzers writes one.
> `tests/unit/test_method_docs_match_the_code.py` now checks each claim against
> the module it names, so a header that stops being true fails the suite rather
> than misleading a reader.

---

## The problem

Custodial services issue a fresh deposit address per user, often per deposit.
Those addresses are single-use, appear in no label database, and looking them up
returns nothing.

"Nothing" then gets read as "not a service" — and that is wrong in the worst
direction, because it makes a trace look like it ended when the funds actually
walked into an exchange.

## The technique

Deposit addresses do not hold funds. They sweep to a shared hot wallet, usually
within hours. So rather than asking *what is this address*, ask *where do these
addresses send their money*, and let the convergence point identify the service.

```
seed ──> addr A ─┐
seed ──> addr B ─┼──> 0xf1da…       12 single-use addresses,
seed ──> addr C ─┤                  one destination
    …            ─┘
```

Twelve unattributable addresses become one entity with twelve deposits. If the
hub carries a public label, the whole group inherits attribution. If it does
not, you still know they are one service.

## Algorithm

1. Enumerate the seed's outbound value transfers.
2. Verify completeness: on nonce-based chains, the account nonce must equal the
   number of outbound transactions retrieved. A mismatch means the history was
   truncated and every total afterwards is a lower bound.
3. For each distinct destination, enumerate *its* outbound transfers one hop.
4. Group destinations by shared next hop.
5. Keep groups with fan-in ≥ `min_fan_in` (default 3).
6. Resolve each hub against the attribution layer.

One hop, not many. Deposit addresses sweep directly; going deeper mostly picks
up the hub's own downstream traffic, which is enormous and tells you nothing
about your subject.

## When this fails

Read this section before putting a result in a report.

### It produces false positives

- **Self-custody.** One person moving funds between their own wallets and
  consolidating produces exactly this shape, innocently.
- **Shared infrastructure.** Payment processors, bridges, and rollup batchers
  aggregate from many addresses for reasons unrelated to custody.
- **Fan-in of two is not evidence.** Two addresses sharing a next hop is
  ordinary coincidence at any meaningful volume. The default threshold is three,
  and three is not generous.

### It produces false negatives

- **Reused deposit addresses.** Some services assign one permanent address per
  user. Consecutive deposits share it, fan-in never accumulates, and the cluster
  never forms. Absence of a cluster is not absence of a service.
- **Delayed sweeps.** A service that consolidates weekly looks like a set of
  unrelated dead ends if your window is shorter than its sweep cycle.
- **Deliberate evasion.** An adversary who knows this technique can route
  through addresses that never consolidate, or consolidate only after a long
  delay through intermediaries.

### It is truncated by limits

`max_nodes` caps how many destinations are examined. The analyzer records the
truncation in `Result.warnings` — read them. A report saying "funds reached
three services" is identical in shape whether that was the answer or merely
where the search stopped.

## Interpreting the output

| Situation | What you may say |
|---|---|
| Hub has a `HIGH`-confidence `label_confidence` | "Funds reached *Service X*" |
| Hub is unlabelled, fan-in ≥ 3 | "These N addresses belong to one unidentified service" |
| Fan-in is 2 | Say nothing; note it as a lead |
| Warnings mention truncation or incompleteness | State the limit alongside the number |

The analyzer asserts no attribution at all. Structure shows that addresses are
related; it does not show *to whom*, and the analyzer has no way to find out —
so it reports the grouping and quotes whatever the attribution layer already
knew about the hub, keeping the two clearly apart.

`label_confidence` is about the hub's label and nothing else. A cluster with a
`CERTAIN` hub label is a certain identification of *the hub*, not a certain
deposit cluster: everything in "When this fails" above still applies.

## References

- Meiklejohn et al., *A Fistful of Bitcoins: Characterizing Payments Among Men
  with No Names* (2013) — establishes the related co-spend heuristic for UTXO
  chains and the general approach of inferring entities from transaction
  structure.
