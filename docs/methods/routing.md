# Time-respecting routing

**Confidence produced:** none. A route is a *candidate* — what remains after the
impossible has been removed, which is weaker than proof and stronger than a line
on a picture.
**Implemented by:** `chainscope.analysis.route.RouteAnalyzer`

---

## The problem

"How did money get from A to B" is the question every flow-analysis product is
built around, and the one where drawing something plausible is easiest.

The obvious implementation — breadth-first search over the transfer graph — has
no notion of when anything happened. It returns `A → X → B` where X paid B
*before* A ever paid X. Money cannot travel that way.

Measured on a real ledger of 55 transfers between 37 addresses: of 224 multi-hop
shortest paths BFS returned, **138 were causally impossible — 62%**. Asked the
other way, **53%** of the address pairs it calls connected have no
time-respecting route at all.

## The technique

A *time-respecting path*: a walk whose edge timestamps are non-decreasing. The
formulation is standard in temporal graph theory.

Three further refusals, each of which otherwise produces a picture
indistinguishable from a correct one:

**A path through a hub is not a path.** An exchange hot wallet touches
everything, so allowing one finds a route between almost any two addresses — and
it means nothing, because a custodian commingles what it receives. The
structural link is real; the causal link is destroyed. Such routes are withheld
by default and *counted*, never silently dropped.

**A path cannot carry more than its narrowest hop.** A route whose thinnest link
moved 0.001 ETH is not how 1,000 ETH travelled. Every route reports `carries`,
so "a path exists" and "this path could have carried the sum" stay separate.

**A hop in a forged token is not a hop.** A token contract emits its own
`Transfer` events, so a route built from an impersonating token is a route the
attacker drew. Routes are ranked believable-first and a forged one says so.

## Algorithm

1. Expand outward from both endpoints, bounded by `max_expand`, spending the
   budget least-connected first — an address appearing in dozens of transfers is
   a service, whose expansion yields thousands of neighbours and no usable hop.
2. Collapse duplicate transfers: expanding from both ends reads every transfer
   between two expanded addresses twice, and each copy multiplies the routes
   through it.
3. Enumerate walks from source to target where timestamps never decrease, each
   address appearing at most once.
4. Mark each route with the first hub it crosses, its narrowest hop, whether the
   asset changes, and how many hops moved an untrusted asset.

## When this fails

### "No route" is not "no connection"

Funds that crossed a chain, an exchange, or an address the store has never seen
leave no chain of transfers behind. Every empty result says this.

### Every bound changes the answer

`max_hops`, `max_expand` and `per_node` all decide how much was searched, and
all are recorded in `Result.params`. A route whose middle lies outside what was
read does not appear at all.

### Hub detection is over the data in hand

Degree is counted within the store, not across the chain. A store holding one
case sees an exchange's three addresses rather than its three million, so a hub
can be *missed*. It cannot be invented — an address with 25 counterparties here
has at least 25 — so every hub reported is genuinely one.

### A candidate is not a route the money took

Several time-respecting routes usually survive. Which one carried the funds is
not decidable from structure, and this does not pretend otherwise.

## Interpreting the output

| Situation | What you may say |
|---|---|
| Routes found, none crossing a hub | "There is a path consistent with the timing"; quote hops and `carries` |
| Routes crossing a hub | "The ledger connects them via X, but X commingles" — not a money link |
| Route with forged hops | Say nothing from it; it is the attacker's own log entries |
| No route | "None within what was read"; quote the bounds |

## References

- Kempe, Kleinberg & Kumar, *Connectivity and Inference Problems for Temporal
  Networks*, STOC 2000 — the formulation and hardness results.
- Wu et al., *Path Problems in Temporal Graphs*, VLDB 2014 — efficient
  earliest-arrival algorithms.
