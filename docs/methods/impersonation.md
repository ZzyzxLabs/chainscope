# Asset impersonation

**Confidence produced:** `MEDIUM`, and only for the Unicode-based verdicts,
which are emitted as `Hypothesis` objects with their score factors exposed. The
registry-based verdicts — `genuine` and `forged` — are `Finding`s carrying no
confidence at all, because contract identity is settled rather than inferred.
**Implemented by:** `chainscope.analysis.impersonation.ImpersonationAnalyzer`

---

## The problem

A token symbol is a string the deployer chose. `symbol()` returns whatever the
contract was written to return, and nothing on-chain constrains it.

This is the only place in a case where the data is **authored by the adversary**
with the analyst's tool in mind. A block timestamp is not trying to mislead
anybody. A ticker is.

Measured on one real case: the seed address had 55 ERC-20 transfers, and **42
belonged to tokens imitating USDC or ETH**. Six were genuine, and those six were
the answer. Any tool that reports "totals by symbol" over that data reports a
figure built almost entirely out of forgeries — with the same units, the same
magnitude, and the same place in the report as a real one.

## The technique

Three checks. No two of them overlap, and no single one finds all three attacks.

| Symbol | How it works | What catches it |
|---|---|---|
| `UЅDC` | Latin word, one Cyrillic `Ѕ` (U+0405) spliced in | mixed-script |
| `ЕТН` | *Entirely* Cyrillic, so perfectly consistent | confusable skeleton |
| `ETH` | Plain ASCII, simply named after a real asset | canonical registry |

The Unicode half follows [UTS #39, *Unicode Security
Mechanisms*](https://www.unicode.org/reports/tr39/) — mixed-script detection
from §5, confusable skeletons from §4. It is the standard written for this
problem, and using it rather than a hand-rolled lookalike test matters because
the failure mode of the hand-rolled version is that it works on the examples its
author thought of.

The third case is the one Unicode cannot touch, and it is the most common. The
rule the field actually uses is **compare the contract address, never the symbol
string**; `CANONICAL` makes that comparison explicit.

## Algorithm

1. Group the address's transfers by **contract**, never by symbol. Grouping by
   symbol is the bug — two contracts sharing a ticker are two assets, and one of
   them may have chosen that ticker precisely so a query would merge them.
2. For each asset, look up `(chain, symbol)` in `CANONICAL`.
   - Present and the contract matches → `genuine`.
   - Present and it does not → `forged`.
   - Present, empty, and there is no contract → `genuine`: it is the native
     asset, which has nothing to forge.
   - Absent → fall through to the Unicode checks.
3. Compare the skeleton against every canonical symbol on that chain →
   `lookalike`.
4. Test for mixed script or non-ASCII characters → `unknown-script`.
5. Otherwise `unlisted`.

## When this fails

### It cannot see a forgery of something it does not know

`CANONICAL` is a short list of names worth forging, not a token list. A general
token list would need constant maintenance and would make every unlisted token
look suspicious, which inverts the error this exists to prevent.

So **`unlisted` is not a clean bill**. It means the canonical check had nothing
to compare against, which is the ordinary state for most real tokens.

### The character table is partial

`TO_ASCII` covers Cyrillic, Greek, Armenian, Cherokee, fullwidth forms and
mathematical alphanumerics — the blocks that impersonate ASCII in practice. A
symbol built from a block not in it will not produce a skeleton match, though it
will usually still trip the non-ASCII check.

Digits that resemble letters (`0`/`O`, `1`/`l`) are deliberately **not** folded.
Both forms are legitimate in a ticker, and folding them would report a real
symbol as a forgery.

### It says nothing about intent

A token may share a name with a real one by accident, or because it is a
testnet deployment, or a fork. The finding is that the symbol is not evidence of
identity — not that somebody set out to defraud.

## Interpreting the output

| Situation | What you may say |
|---|---|
| `genuine` | "This is USDC" — contract identity, the strongest statement here |
| `forged` | "This claims to be USDC and is not"; quote the contract |
| `lookalike` | "This folds to X under UTS #39"; quote the codepoint |
| `unknown-script` | "This symbol contains characters outside ASCII" — **not** that it renders like anything in particular. Whether a reader would confuse it depends on the font, and this does not know |
| `unlisted` | Say nothing about it either way |

Every finding carries the codepoint and Unicode name of each suspect character,
so a reader can verify the claim rather than trust it. Quote them: "position 1
is U+0405 CYRILLIC CAPITAL LETTER DZE" is checkable, and "this token is fake"
is not.

**Nothing is filtered.** Both the forgeries and the genuine assets are reported.
A tool that silently dropped what it judged fake would be making the same kind
of unreviewable decision as one that silently kept it, and would be much harder
to argue with.

## References

- Unicode Consortium, *UTS #39: Unicode Security Mechanisms* — mixed-script
  detection (§5) and confusable skeletons (§4).
