# Is the money arriving at this address safe to accept?

The design for deposit screening. Written after reading what the field has
actually tried, because three of the obvious choices here are known to be wrong
and one of them has already produced a regulatory penalty.

## The question, stated precisely

Not "is this address bad". That question has no answer and asking it is how
victims get frozen. The question is:

> **This specific value, arriving at this address at this time — what is it
> exposed to, how directly, on whose word, and what should we do about it?**

Five separable parts. Every one of them has to survive being asked about
individually, because that is the form the challenge takes six months later.

## What the literature settles

### Taint: three algorithms, and the choice is legal rather than technical

When tainted and clean funds mix, which output carries the taint?

| method | rule | effect |
|---|---|---|
| **poison** | any tainted input taints every output entirely | taint grows without bound; after a few hops most of the chain is "tainted" |
| **haircut** | outputs carry the input's tainted *proportion* | taint dilutes to homeopathic fractions that never reach zero |
| **FIFO** | first satoshi in funds the first satoshi out | taint stays a bounded, specific quantity |

Most commercial tools use **haircut**. The academic comparison finds **FIFO**
gives more precise results, and FIFO has something none of the others do: it is
**the rule English law already uses for mixed funds**, from *Devaynes v Noble*
(Clayton's Case, 1816), which set first-in-first-out for withdrawals from a
mixed banking account. Cambridge's group built `RustyTaintChain` on exactly
that reasoning.

That matters far more than the accuracy difference. A customer defending a
freeze can say *"we applied the same rule a court would apply to a mixed
account, and here is the 1816 authority"*. They cannot say that about a
proportional haircut, which is a convention with no legal counterpart.

**Decision: FIFO is the default. Haircut and poison are selectable, and the
decision record always names which ran.** A screening result that does not say
which taint model produced it is not reproducible, because the three disagree.

### Stop at services, and say that you stopped

Tracing *through* an exchange is unreliable and the reason is structural:
custodial services use omnibus accounts, so funds from thousands of unrelated
customers pass through the same addresses. A path that continues through a hot
wallet is not a path — it is an artefact of everyone sharing a bucket.

The right behaviour is to stop when a trace reaches a known service and assess
the risk up to that point. `Category.is_terminal` already exists in this
codebase for exactly this, and `linked_holders` already treats a service as a
boundary rather than an edge. What is missing is carrying "the trace stopped
here, and the money continued somewhere we cannot see" into the *decision*,
rather than only into the drawing.

### Hop distance is not a risk multiplier, and there is no safe threshold

Direct exposure (the counterparty itself) and indirect exposure (a counterparty
of a counterparty) are different claims and no institution treats them alike.
But two things are commonly got wrong:

**Nobody has set a universal hop threshold.** OFAC has published no de minimis
level. There is no number to point at.

**Setting the threshold too high has already been penalised.** The NYDFS
enforcement against Block turned in part on internal thresholds — even a 1%
exposure to terrorism-linked wallets was not defensible as "below our
tolerance".

So the threshold is unavoidably the customer's own risk decision, and the only
thing a tool can do that helps is make that decision **explicit, versioned, and
attributable**. A vendor's default threshold, applied silently, is precisely
the thing that failed.

**Decision: thresholds live in a named, versioned policy the customer owns.
Every decision records the policy version that produced it, and changing a
policy never rewrites past decisions.**

### Do not build a GNN for this

The tempting move is graph learning, and the Elliptic dataset (200k Bitcoin
transactions, 49 time steps) is the standard benchmark for it. Two results
argue against:

- In Weber et al.'s original 2019 paper, **Random Forest outperformed the graph
  convolutional network.**
- A 2026 re-evaluation, *When Graph Structure Becomes a Liability*, finds that
  under temporal distribution shift GNNs degrade badly while feature-based
  models stay stable — Bitcoin's connectivity patterns are not temporally
  stable enough to learn from. The graph structure becomes the liability.

And the illicit class is under 2% of the data, which is a regime where a model
can reach excellent headline accuracy while being useless.

Add the reason that would apply even if the numbers were good: **a score nobody
can interrogate is the one thing our commercial position is against.** If a
learned component ever enters this system it will rank items for human review
and produce evidence, never a decision.

## Architecture

```
                    ┌──────────────────────────────────────┐
   deposit event    │  1  RESOLVE                          │
   (address, asset, │     address → entity, function       │  core/entity.py
    amount, time)   │     control-at-time check            │  (built)
        │           └──────────────────────────────────────┘
        ▼                            │
   ┌──────────────────────────────────────┐
   │  2  TRACE BACK                       │   analysis/taint.py (exists)
   │     FIFO, bounded hops               │   + stop at is_terminal
   │     stop at services, record it      │   + record the boundary
   └──────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────┐
   │  3  EXPOSURE                         │   NEW: risk/exposure.py
   │     per source: amount, hops,        │
   │     direct|indirect, role, evidence  │
   │     never one number                 │
   └──────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────┐
   │  4  POLICY                           │   NEW: risk/policy.py
   │     customer-owned, versioned rules  │
   │     first matching rule wins         │
   └──────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────┐
   │  5  DECISION                         │   NEW: risk/decision.py
   │     allow | hold | reject | escalate │
   │     | enhanced_kyc | report          │
   │     + rule fired + counterfactual    │
   │     + unreachable sources            │
   │     + attestation                    │
   └──────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   6  RE-SCORE              7  ACT
   new intel over           correspondence.py (exists)
   old decisions            freeze request + clock
```

### 3. Exposure is a list, never a number

```python
Exposure(
    source=Entity(...),          # who
    role=RoleKind.ATTACKER,      # in what capacity, per incident
    incident="lpdfi-2026-08",
    amount=Amount(...),          # how much of *this* deposit, by FIFO
    share=0.31,                  # of the deposit
    hops=3,                      # how far
    path=(...),                  # which addresses
    stopped_at_service=False,    # did the trace end because we stopped
    evidence=(Attribution(...),) # whose word, at what confidence, when
)
```

A single scalar cannot carry any of that, and the scalar is what gets
challenged. `risk_score: 82` is not an answer to "why".

### 4. Policy is data the customer owns

```yaml
name: acme-exchange-deposits
version: 4
effective_from: 2026-08-01T00:00:00Z
rules:
  - id: sanctions-direct
    when: {role: [attacker], category: [sanctioned], hops: {max: 0}}
    then: reject
    because: "OFAC has published no de minimis exposure level."
  - id: sanctions-indirect
    when: {category: [sanctioned], hops: {max: 3}, share: {min: 0.0}}
    then: escalate
    because: "NYDFS/Block: a low-percentage threshold was not defensible."
  - id: mixer-recent
    when: {category: [mixer], hops: {max: 2}, age: {max_days: 30}}
    then: enhanced_kyc
  - default: allow
```

First matching rule wins, and the fired rule's `id`, `because` and the policy
`version` travel in the decision. Ordered rules rather than weights, because
"rule `sanctions-direct` fired" is defensible and "the weighted sum crossed
0.78" is not.

### 5. The decision, and its counterfactual

The part no closed vendor offers:

```
decision:     hold
rule:         mixer-recent (policy acme-exchange-deposits v4)
exposure:     0.31 of this deposit, 2 hops, via Tornado Cash
              evidence: OFAC SDN list, CERTAIN, observed 2026-07-14
counterfactual:
              without the OFAC tag on 0x8589…6d2f this would be `allow`
incomplete:   eth_labels could not be read; a source failing is not a clean
              screen
attestation:  sha256:… — 14 responses, re-runnable
```

The counterfactual is cheap to compute (re-run the policy with each evidence
item removed) and it is the single most useful line for the person who has to
defend the decision or explain it to the customer.

### 6. Re-scoring is the subscription

A tag landing today changes the risk of a deposit accepted last week. `watch`
already evaluates rules over new blocks; this is the transpose — **evaluate new
intelligence over old blocks** — and it is the part that cannot be replaced by
asking a model, because the value is in having been running continuously.

Every re-score is a new decision referencing the old one. Decisions are
append-only: what was believed in March stays readable in September, because
"what did you know at the time" is the question that gets asked.

## What to get wrong carefully

**Deposit addresses.** Value into an exchange hot wallet identifies nobody;
value into a deposit address identifies one customer. `Function` in
`core/entity.py` already separates them. Screening that treats them alike makes
every customer of an exchange look like the exchange.

**Victims.** Money leaving a theft touches the victim, the attacker and
everything in between. `RoleKind` refuses to let those collapse, and the
default for "money arrived" is `RECIPIENT`, not `LAUNDERER`.

**Control changes.** A compromised exchange keeps its addresses.
`controlled_at` is checked against the *time of the deposit*, not now.

**Time.** A sanctions listing dated after the deposit is not exposure at the
time of the deposit. It may still be reportable — that is a policy question,
and the policy should have to say so explicitly rather than inherit it from a
join that ignored dates.

**Silence.** A source that could not be read makes the screen incomplete. The
existing `reliable` / `unreachable_sources` fields already carry this; the
decision must refuse to say `allow` on the strength of a source that never
answered.

## Order of work

1. `risk/exposure.py` — the typed exposure list. Everything else consumes it.
2. FIFO taint over the store, with the service boundary recorded.
3. `risk/policy.py` — versioned rules, ordered, customer-owned.
4. `risk/decision.py` — the record, with the counterfactual.
5. `chainscope screen` and `POST /screen`.
6. Re-scoring on new intelligence.

## Sources

- Taint methods and FIFO/Clayton's Case: [Probing the Mystery of Cryptocurrency
  Theft: An Investigation into Methods for Taint
  Analysis](https://www.arxiv.org/pdf/1906.05754v1); reference implementation
  [TaintChain/RustyTaintChain](https://github.com/TaintChain/RustyTaintChain)
- Elliptic dataset and GCN baseline: [Anti-Money Laundering in Bitcoin:
  Experimenting with Graph Convolutional Networks for Financial
  Forensics](https://arxiv.org/pdf/1908.02591)
- GNN brittleness under temporal shift: [When Graph Structure Becomes a
  Liability](https://arxiv.org/pdf/2604.19514)
- Extended labelled dataset: [Demystifying Fraudulent Transactions and Illicit
  Nodes in the Bitcoin Network](https://arxiv.org/pdf/2306.06108)
- Indirect exposure and hop treatment: [TRM Labs, Indirect
  Risk](https://www.trmlabs.com/glossary/indirect-risk); [Elliptic, Sanctions
  screening and
  hops](https://www.elliptic.co/blog/analysis/sanctions-screening-and-hops-in-crypto-transactions-ensuring-detection-of-sanctions-risks)
- False positives from omnibus/deposit-address structure: [Elliptic, How to
  reduce AML false positives in
  crypto](https://www.elliptic.co/blog/how-to-reduce-aml-false-positives-in-crypto)
