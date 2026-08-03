# Inflow attribution

**Confidence produced:** none. It decomposes a total and refuses to correct one.
**Implemented by:** `chainscope.analysis.contributors.ContributorsAnalyzer`

---

## The problem

A deposit address is identified as the subject's, its inbound total is computed,
and the figure goes in the report.

But a deposit address is a **destination, not a private channel**. Anybody may
pay it. In the case this check comes from, one such address had also received
**3 ETH from a completely unrelated party** — an address with 122 transactions
of its own, funded from a different 500 ETH, and itself a victim of the same
poisoning campaign.

Included, it inflates "the subject sent N ETH to this service" by exactly 3 ETH.
Nothing about the resulting number looks wrong.

## The technique

Split the inflow by contributor and classify each one's relationship to the
subject:

| Bucket | Meaning |
|---|---|
| `self` | The subject itself. The uncontroversial part of any total |
| `reachable` | A time-respecting route runs from the subject to it — see [routing](./routing.md) |
| `co_funded` | Shares a first funder with the subject |
| `unlinked` | Nothing in this store connects it to the subject |

## Algorithm

1. Sum inbound transfers to the target, grouped by sender.
2. For each sender other than the subject, search for a time-respecting route
   from the subject, bounded by `max_hops`.
3. Failing that, compare first funders — first *seen in this store*, which is
   weaker than first ever and is the only claim the data supports.
4. Report the buckets separately, with the bound stated.

## When this fails

### `unlinked` does not mean unrelated

This is the one misreading that matters. An address related to the subject
through a hop nobody fetched lands in `unlinked` beside a genuine stranger, and
absence of a link cannot distinguish them. The bucket is named for what is true
of it.

Every report states how far the search actually went, because "no link found"
over a store holding two hops is a different statement from the same words over
a store holding twenty.

### `co_funded` is much weaker than it looks

An exchange funds thousands of unrelated customers.
`chainscope.analysis.funding` measures this directly: with the service guard
off, precision drops to **0.7%**. It is reported separately for that reason and
is never counted as the subject's.

### It cannot see contributions outside the store

A payer whose transfers were never fetched does not appear at all, so the total
here is a total over what was read.

## Interpreting the output

| Situation | What you may say |
|---|---|
| All `self` / `reachable` | "The subject sent N" — the sum is safe |
| Some `unlinked` | "The subject sent *at least* the attributable figure"; name the rest |
| Some `co_funded` | Say so separately; do not fold it into either figure |

**Nothing is subtracted.** Producing "the corrected total" would hide the
judgement inside a number, and the judgement — whether an unlinked contributor
is a stranger or an unexplored hop — belongs to the reader, who has context this
code does not.
