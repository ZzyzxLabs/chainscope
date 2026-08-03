# Address poisoning

**Confidence produced:** none. It reports groups and the probability that they
arose by chance, and explicitly refuses to nominate the real address unless one
signal — a payment in a trustworthy asset — distinguishes it.
**Implemented by:** `chainscope.analysis.poisoning.PoisoningAnalyzer`

---

## The problem

The attack costs nothing and targets attention rather than cryptography.

An attacker grinds a vanity address matching the first few and last few
characters of one the victim actually transacts with, then sends it a zero-value
or dust transfer so it appears in the victim's history. Later the victim copies
"the address I sent to last time" out of that list — reading, as everybody does,
the first four and last four characters — and pays the attacker.

## The technique

Group the addresses seen in a subject's history by `(first N, last N)` and
report every group with more than one member.

**The arithmetic is the finding.** Matching 4 hex characters at each end is 32
bits. Across *n* addresses there are `n(n-1)/2` pairs, so the expected number of
chance collisions is `pairs / 2**32` and the probability of seeing any is
`1 - exp(-expected)`.

Measured on one real case: 36 counterparties, **nine** groups, against a
coincidence probability of **1.5 × 10⁻⁷**. Nine is not luck; it is proof of
grinding.

`chance_of_collision` computes this for whatever set is in front of the reader,
because "these look similar" invites "coincidences happen" — and they do, at a
rate a report has to state.

## Algorithm

1. Fold the subject's transfers into one record per counterparty.
2. Determine which assets can be trusted to report honestly, via
   [`impersonation`](./impersonation.md). See below — this step is not optional.
3. Bucket counterparties by `(prefix, suffix)`; keep buckets with 2+ members.
4. Within a group, a member is *paid* only if the subject sent to it **in a
   transfer of a trusted asset**.
5. Exactly one paid member and at least one unpaid → decidable. Otherwise the
   group is reported undecided.

## When this fails

### The evidence can be authored by the attacker

**This is the failure that matters, and the first version of this analyzer had
it.** A token contract emits its own `Transfer` events, so a forged token can
log a transfer that never happened — including one claiming the *victim* paid an
address of the attacker's choosing.

Run against the real case above, the naive version announced "the subject paid 4
of these 5", which was false and was the most persuasive thing it could have
said. Of the 27 addresses in a lookalike group there, **24 appear only in
forged-token transfers**.

So evidence from an asset that fails the impersonation check counts for nothing.
The claim is still reported — a reader may meet the same transfer elsewhere —
but named as a claim by the attacker.

### It usually cannot tell you which one is real

That is the intended behaviour, not a shortfall. Naming the wrong address is how
somebody's next payment reaches the attacker, so where nothing distinguishes a
group's members the analyzer refuses to nominate one.

### Repetition and dust are reported, not counted

An address appearing once, or moving only zero-value transfers, looks like
poisoning. Neither enters the verdict: an address can legitimately appear once,
and a real payment can legitimately be small. They are printed for the reader,
who has context this code does not.

### A short window hides the pair

The subject may have paid the genuine address before the window, leaving only
the impostor visible inside it. Absence of a paid member is reported as
undecided, never as "the one that remains is real".

## Interpreting the output

| Situation | What you may say |
|---|---|
| Groups found, probability stated | "These addresses were generated to be confused"; **quote the probability** |
| One paid member, in a trusted asset | "The subject paid X; the others only ever appeared inbound" |
| No paid member, or several | Say which addresses are involved and that it is not decidable |
| Every payment claim from a forged token | "Nothing here is evidence of a real payment" |

Do not copy an address out of a transaction list in a case where this fires.

## References

- The birthday bound, applied to the truncated display form. The parameters that
  matter are how many characters a wallet UI shows and how cheap grinding is;
  both are stated in `DEFAULT_EDGES`.
