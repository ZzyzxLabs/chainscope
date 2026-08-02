# Security Policy

## Reporting a vulnerability

**Do not open a public issue.**

Report privately via GitHub's [private vulnerability
reporting](https://github.com/chainscope/chainscope/security/advisories/new),
or email **security@chainscope.dev**.

Please include: what you found, how to reproduce it, and what an attacker gains.
A proof of concept helps but is not required to file.

**What to expect:** acknowledgement within 3 working days, an assessment within
10, and credit in the advisory unless you prefer otherwise. If we disagree that
something is a vulnerability, we will say so and explain why rather than let the
report go quiet.

Supported for fixes: the latest minor release. During `0.x`, that means the
latest release only.

---

## What counts as a vulnerability here

chainscope is a read-only analysis tool, so the usual web-application threat
model does not map cleanly. These do apply:

### Any write path to a chain

**Critical, always.** The transport layer is designed so that signing and
broadcasting are not expressible — `Query` is a closed union of read operations
and the wire layer rejects `eth_send*`, `eth_sign*`, `personal_*`, `miner_*`,
and `admin_*`.

If you find a way to make chainscope broadcast a transaction, sign a message, or
otherwise mutate chain state, that is a critical finding regardless of how
contrived the path. The whole point is that a forensics tool cannot move funds.

### Credential leakage

API keys reaching disk, logs, cassettes, case bundles, or a third party.
Recorded test cassettes are a particular risk: they capture real requests, and a
key left in one gets committed to a public repository.

### Code execution via untrusted input

The realistic vector is a hostile response from a data provider, or a case
bundle received from someone else. Case bundles are explicitly designed to be
shared, so treat them as untrusted input — deserialisation that can execute code
is a vulnerability.

### Attribution poisoning

Getting a false attribution into the resolver with elevated confidence — for
example, making a heuristic result present as `CERTAIN`, or bypassing the
constructor checks that require a `source` and a `rationale`.

This matters more than its severity rating suggests. The output of this tool
reaches journalism and litigation; an attacker who can quietly upgrade the
apparent confidence of a claim can get a real person accused. Treat it as a
security property, not a correctness nit.

### Cache poisoning

Making one query return another's cached response, or making immutable-class
entries writable after the fact. A tampered cache produces a *reproducible*
wrong answer, which is worse than a flaky one — it survives being checked.

---

## What does not count

- **Rate limiting or ToS violations against third-party APIs.** Report those to
  the API operator. chainscope throttles by default; if you disable that, the
  consequences are yours.
- **Incorrect heuristic output.** Clustering and cross-chain matching are
  probabilistic by design and documented as such. A wrong result is a bug, or an
  expected limitation — file an issue. It becomes a security matter only if the
  tool *misrepresents its confidence*.
- **Denial of service against your own machine** by running an unbounded
  analysis. Use the depth and node limits.

---

## For users of this tool

Two things worth saying plainly.

**Your queries are visible to your providers.** Every address you look up is
disclosed to whichever API answered. If the subject of an investigation should
not learn that they are being investigated, consider your provider's logging and
jurisdiction. Running your own node is the only way to avoid this entirely.

**Case bundles contain your entire investigation.** They are designed to be
shareable and reproducible, which means they carry every query you ran and every
result you got. Review one before you send it to anyone.
