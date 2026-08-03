# Why this is Python, and what would change that

A blockchain forensics tool that reads whole chains sounds like a Rust or Go
project. This one is not, and the question deserves a measured answer rather
than a preference — so here are the measurements, the reasoning, and the
specific result that would overturn it.

All figures below were taken on the machine this was developed on
(Apple Silicon, Python 3.12, SQLite via the stdlib driver). Reproduce with
`scripts/bench.py`.

---

## 1. The thing everything waits on is the network, by three orders of magnitude

| Path | Throughput |
|---|---|
| Uncached provider round trip | **35 transfers/s** |
| Local store write | 85,952 rows/s — **2,451×** the network |
| Local store read | 148,264 rows/s — **4,228×** the network |

A single uncached Blockscout call for one address takes ~1.6 seconds and returns
55 transfers. In that time the local layer could have written 137,000 rows.

This is not a Python fact. It is an *any language* fact: the remote index is
rate-limited, paginated, and on the other side of the Atlantic. Making the local
half four times faster changes a 1.6-second wait into a 1.6-second wait.

## 2. Most of the local hot path is already compiled

Profiling a 60,000-row write:

| | share of the write path |
|---|---|
| `sqlite3.executemany` (C) | 64% |
| `sqlite3.commit` (C) | 10% |
| **Python, all of it** | **26%** |

The largest single Python cost is `chains.address_key` at about 10% — the
function that decides how addresses compare, which is the last thing anybody
should want to rewrite in a language with different string semantics.

**A perfect rewrite of every line of Python in the write path buys at most
1.35×.** On a path that is already 2,451× faster than the thing it waits on.

## 3. The graph work, where a compiled language helps most, is not slow either

| Transfers in the ledger | `route` | `poisoning` |
|---|---|---|
| 50,000 | 0.12 s | 0.05 s |
| 200,000 | 0.41 s | 0.19 s |

Linear, and sub-second at a size larger than any single case this tool has been
pointed at. The bounds these analyzers carry (`max_hops`, `max_expand`,
`max_steps`) exist to stop *combinatorial* blowup, which is an algorithm
property and would blow up identically in Rust.

---

## What Python actually buys here

Not developer comfort — two things specific to this problem:

**Provenance lives in the type system.** `Attribution` cannot be constructed
without a `source`; a claim below `MEDIUM` cannot be constructed without a
`rationale`; `Hypothesis` cannot exceed `MEDIUM`. These are runtime refusals in
`__post_init__`, and they fire on data that arrived from a provider at 3am. A
compiled language checks more at compile time and *less* at the boundary where
untrusted data enters, which is exactly where this tool needs the check.

**Plugins are the distribution model.** A third-party analyzer is an entry
point in a `pyproject.toml`. In Go that is a shared object or a subprocess
protocol; in Rust it is a dynamic library with an ABI to keep stable. The
project's goal is that somebody else can build their own analysis on top of it,
and the cost of that must be one file, not a toolchain.

## What would change the decision

Any one of these, measured rather than assumed:

1. **A store an order of magnitude larger.** If a case reaches ~10 million
   transfers, the read path — which is Python object construction, and the one
   place Python genuinely dominates — becomes the cost. The fix there is a
   columnar read path before it is a new language.
2. **Sustained ingest rather than bursts.** A live watcher across many chains
   shifts the balance from latency to throughput.
3. **An analyzer that is genuinely CPU-bound.** Clustering over tens of millions
   of edges, or anything with a super-linear kernel. None of the current
   fourteen is.

The answer to (1) and (3) is likely a **compiled extension for the one hot
path**, not a rewrite: `pyo3` or a C module behind the same interface, keeping
the plugin surface and the type-level provenance intact. That is a smaller
change than a port and reversible if the measurement was wrong.

## What was actually done instead

The measurements above pointed at cheaper wins, which were taken:

- The transport layer caches by content with finality-derived TTLs, so the
  1.6-second call happens once.
- Enumerations refuse to return silently-truncated results rather than being
  fast about being wrong.
- `address_key` is cached per adapter, since it sits on every write.

Speed here is mostly a question of *not asking the network twice*, and that is
a design property, not a language one.
