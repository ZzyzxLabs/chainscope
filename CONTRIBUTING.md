# Contributing to chainscope

Thanks for considering it. This document is short on ceremony and specific about
the two or three things that actually matter here.

---

## Setup

```bash
git clone https://github.com/chainscope/chainscope
cd chainscope
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest -q          # 59 tests, no network, should take under a second
```

Before opening a PR:

```bash
ruff check src tests && ruff format src tests
mypy
pytest -q
```

---

## The rules that are not negotiable

Everything else is a preference. These three are not.

### 1. Tests never touch the network

The suite blocks sockets (`tests/conftest.py`). A test that reaches for the
network fails loudly with an explanation.

If you need real data, **record a cassette** under `tests/cassettes/` and replay
it. If you genuinely need a live call — verifying that a provider's API still
behaves as expected, say — mark it `@pytest.mark.network`. Those are deselected
by default and never run in CI.

This is not fussiness. A suite that fails on someone else's machine for reasons
they cannot reproduce loses you contributors faster than any missing feature.

### 2. Attribution claims carry their provenance

You cannot construct an `Attribution` without a `source`, and you cannot
construct one at `Confidence.LOW` or below without a `rationale`. Do not work
around this. It is the point of the project.

When you write code that produces attributions, pick the confidence level
honestly:

| Level | Use when |
|---|---|
| `CERTAIN` | An authoritative list says so, or the contract names itself on-chain |
| `HIGH` | A third party publishes the label (block explorer nametag) |
| `MEDIUM` | A documented structural heuristic derived it (clustering, consolidation) |
| `LOW` | Behavioural inference — timing, amounts, fee patterns |
| `SPECULATIVE` | A single coincidence |

If you find yourself wanting to bump a level so the output looks more
authoritative, that is precisely the failure mode this design exists to prevent.

### 3. A new attribution source updates `docs/data-sources.md` in the same PR

Publisher, license, redistribution terms, and the confidence level it maps to.
CI fails the build if a source module has no matching row.

An attribution source whose provenance nobody wrote down becomes, three tools
later, a fact nobody can trace back to anything.

---

## Adding things

The architecture is measured by a single benchmark: **adding a chain, a
provider, an analyzer, or an attribution source should take one file and one
test cassette.** If your change needs edits to `core/`, either the abstraction is
wrong or you have found a genuine gap — say so in the PR and we will look at the
abstraction rather than papering over it.

See [docs/extending.md](docs/extending.md) for worked examples of each.

Everything registers through entry points, so third-party packages extend
chainscope without being merged into it. You do not need our permission to ship
an extension, and a plugin living in its own repository is a perfectly good
outcome.

---

## Analysis code: two specific expectations

**Heuristics return `Hypothesis`, not answers.** Cross-chain matching, change
detection, and clustering are inherently probabilistic. Return scored candidates
with the factor breakdown exposed, and populate `alternatives`. A caller who can
see *why* something ranked first can catch your mistake; one who gets a bare
answer cannot.

**Document how your method fails.** Every technique in this field has conditions
under which it silently produces garbage — CoinJoin defeats co-spend clustering,
exchange batching defeats consolidation analysis, and any of them can be defeated
deliberately by an adversary who knows they are watched. If your `docs/methods/`
page has no "when this fails" section, the review will ask for one.

---

## Commit messages and PRs

Explain **why**, not what. The diff already shows what.

A commit that says "fix precision bug" is less useful in two years than one
saying that `Decimal` defaults to 28 significant digits, so wei balances above
~10²⁸ silently lost their low-order digits, and that a property test caught it.

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`) are used
but not enforced by tooling.

---

## Style

`ruff` and `mypy --strict` decide the mechanical questions; don't argue with
them in review. Beyond that:

- **English** for code, comments, docstrings, and CLI output. Translated READMEs
  are welcome and appreciated.
- **Comments explain reasoning**, not mechanics. `# increment counter` is noise;
  `# sort_keys matters: callers building the same query with differently ordered
  kwargs must hit the same entry` is the reason someone will not break it later.
- **Public functions get docstrings** with a sentence on why they exist, not
  only what they do.

---

## Scope

Things that fit: chains, providers, attribution sources, analysis techniques,
renderers, performance, documentation.

Things that do not, and why:

| Not this | Because |
|---|---|
| Anything that signs or broadcasts | Read-only is an architectural property. See ARCHITECTURE.md §4.2 |
| Vendored proprietary label data | Licensing, and it would misrepresent what this project is |
| Real-time monitoring / alerting | Different latency and durability tradeoffs; would distort the design |
| Producing regulatory filings | Compliance liability should not live in a tool |

If unsure, open an issue before writing code. Rejecting a large PR on scope
grounds is a bad experience for everyone and easily avoided.

---

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).

---

## A note on what this tool is used for

chainscope output can end up in journalism, in litigation, and in decisions that
affect people's lives. That shapes what "good code" means here: a fast heuristic
that is wrong 5% of the time without saying so is worse than a slow one that
reports its uncertainty.

Write for the analyst who will have to defend your output to someone hostile.
