# What this project is for

A strategy note, written because the obvious version of this tool is now
worthless and it is worth saying exactly why.

## The thing that stopped being defensible

A competent model can write fund-tracing code. It can call an RPC, walk a
transfer graph, lay it out, and write the report. Anything whose value is
*"turn an address into a picture and a paragraph"* is now a prompt, and the
price of a prompt goes to zero.

So the parts of this project that are a graph renderer and a query language are
not the project. They are the interface.

What a model cannot do from a standing start is **know things that are not on
the chain**, **keep knowing them as they change**, and **be accountable when
the answer is wrong**. That is the whole of what follows.

## What customers actually buy

Nobody pays to learn that address A sent 100 USDT to address B. They pay to
decide:

- Can this deposit be credited?
- Should this withdrawal be held?
- Does this customer need enhanced KYC?
- Is this reportable?
- Which exchange do we contact, and by when?

The product is a **decision with consequences attached**. Every layer below it
exists to make that decision defensible:

```
raw chain data
  → attribution        who is this, and how do we know
  → entity             which addresses are one party
  → exposure           what has this touched, how directly
  → decision           allow / hold / reject / escalate / report
  → action             the freeze request, the evidence pack, the clock
  → record             what we decided, on what, under which policy, when
```

The bottom four are where the money is and where the work is.

## Why this project cannot win the usual way

The incumbents' moat is **proprietary attribution plus a customer network plus
law-enforcement relationships**. Chainalysis has spent a decade and a great
deal of money building a label set nobody can replicate, and a position where
exchanges and agencies route work through them.

An open-source project cannot outspend that, and pretending otherwise is how
this ends up as a worse Chainalysis with no customers. **We will not win on
having more labels.**

So the strategic question is not "how do we get their data". It is: *what is
structurally impossible for a closed vendor to offer, that a regulated customer
badly wants?*

## The answer: be the decision record, not the dataset

A compliance officer's worst day is not a missed alert. It is being asked, by a
regulator or a court or a customer's lawyer, **why** an account was frozen —
and having only a vendor's score to point at.

A closed vendor structurally cannot answer that. Their scoring is the product;
opening it destroys the product. The best they can offer is a category, a
number, and an assurance.

That is the gap. The position this project should take:

> **Every decision is reproducible, attributable to its sources, and explainable
> as a counterfactual — and the customer can read the rule that produced it.**

Concretely, a screening result should be able to say:

- these tags drove it, from these sources, at these confidences, observed on
  these dates;
- this policy, at this version, fired this rule;
- the exposure was indirect, three hops, through this path;
- had tag *X* been absent, the decision would have been *allow*;
- these sources could not be reached, so the result is incomplete **and says
  so**;
- here is the attestation — re-run it and every input hashes the same.

None of that requires owning the labels. It requires the substrate to be
honest, and this codebase is already built that way: `Attribution` cannot exist
without a source, a `Hypothesis` cannot claim more than MEDIUM, `attest` binds
a figure to the queries that produced it, and an absence is never rendered as a
result. Those were written as engineering principles. They are the commercial
position.

**We are not competing on the data. We are competing on whether the answer
survives being questioned.** A vendor whose score is a trade secret cannot
follow us there.

## The second position: federation, not ownership

If we do not own attribution, we should be the place where everyone else's
attribution composes.

A serious customer already has: a vendor feed, a public set like the GraphSense
TagPacks, sanctions lists, and their own private tags — the most valuable of
the four and the one no vendor sees. Today those live in four places and get
reconciled by a human.

What is missing is the **aggregation contract**: many sources with different
confidences, licences, and freshness, combined into one entity view, with the
record of *which source drove which decision* preserved through to the
decision. Including the licence, because redistributing a vendor feed inside an
evidence pack is a contract breach and the tool should know that before the
customer finds out.

The moat here is not the data. It is being the format everyone's data lands in,
and the trust that we do not quietly claim other people's tags as our own.

## What this changes about the project

### Stop treating the graph as the product

The graph is a debugging interface for humans and a presentation surface for
reports. It is not what anyone pays for. It should stay good and it should stop
receiving the majority of the effort.

### Build the entity layer

Today: `address → attribution`. That is raw material.

Needed: `address set → entity → entity type → risk events`, with:

- which addresses belong to one exchange, and which of those are deposit
  addresses versus hot wallets versus consolidation addresses (the shapes
  `consolidation` and `linked_holders` already detect);
- whether an entity's *controller* has changed — a hacked exchange's addresses
  are the same addresses;
- whether an address is an attacker, a victim, or an intermediary, which are
  three completely different facts that a single "involved in incident X" tag
  destroys.

That last distinction is the one that most often produces a wrongly frozen
account, and it is a modelling decision, not a data problem.

### Build the decision, with its counterfactual

A `screen` call returning a decision, an exposure breakdown, the rule that
fired, the policy version, and what would have changed the answer. Tunable per
customer, because a bank and a memecoin exchange have different tolerances and
a single global score serves neither.

### Build retroactive re-scoring

Risk is not evaluated once. When a tag lands — an address identified as an
attacker three days after a customer deposited from it — every historical
exposure must be recomputed and the newly-material ones raised. `watch` already
evaluates rules over new blocks; the missing half is evaluating **new
intelligence over old blocks**.

This is also the most defensible recurring revenue: it cannot be replaced by
asking a model, because the value is in having been running continuously.

### Finish the action path

`correspondence` already records what was asked of whom and when it is overdue.
What is missing is knowing *whom to ask*: the mapping from a deposit address to
the VASP that controls it, and from that VASP to a contactable abuse or
compliance channel. Tracing that ends at "the money reached an exchange" has
stopped one step short of being useful.

## What we deliberately do not build

**A proprietary label set sold as a subscription.** It contradicts the
federation position and we would lose.

**A risk score that cannot be explained.** A model producing a number nobody
can interrogate is the exact thing our position is against, however good the
number is. Any learned component must output evidence, not a scalar.

**Automated freezing.** The tool prepares the request and records the decision.
A human presses send. The liability for freezing a real person's money is not
something a tool should absorb quietly, and a customer who wanted that
automated has misunderstood their own regulator.

## Order of work

1. **Entity layer** — everything else needs it, and nothing else can be
   honest without it.
2. **Decision record** (`screen`) — the artifact customers buy, with the
   counterfactual and the policy version from day one, because retrofitting
   explainability never happens.
3. **Retroactive re-scoring** — turns a tool into a subscription.
4. **VASP directory and evidence packs** — turns a finding into a recovery.
5. **Federation** — multi-source attribution with licence and freshness
   tracking.

The graph, the agent, and the CLI are the interface to all five. They are
finished enough.

## The sentence

> Anonymous chain data into trustworthy entity and risk intelligence, embedded
> in the customer's decision and action flow — and, unlike anyone else selling
> that, **auditable to the bottom**.
