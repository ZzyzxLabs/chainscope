"""Two strings that look the same and are not.

This exists because of a measurement. In one real case the seed address had 55
ERC-20 transfers; 48 of them were impersonation tokens, and the two that
mattered were the six that were not. Any tool that reports "totals by symbol"
over that data reports a number built almost entirely out of forgeries, and it
looks exactly like a number built out of transfers.

The forgeries in that case used three different mechanisms, and no single check
finds all three:

* ``UЅDC`` --- Latin letters with one Cyrillic ``Ѕ`` (U+0405) spliced in. Two
  scripts in one word, which is the signal :func:`is_mixed_script` reads.
* ``ЕТН`` --- *entirely* Cyrillic, and therefore not mixed at all. It is caught
  by mapping each character to what it resembles and comparing the result:
  :func:`skeleton`.
* ``ETH`` --- ordinary ASCII, a token simply *named* after a real one. Unicode
  cannot help here at all; only knowing which contract is the real one can, and
  that lives in :mod:`chainscope.analysis.impersonation`.

The approach follows `UTS #39, Unicode Security Mechanisms
<https://www.unicode.org/reports/tr39/>`_ --- mixed-script detection from §5 and
confusable skeletons from §4. It is the standard written for exactly this
problem, and reaching for it rather than inventing a heuristic is the whole
point: the failure mode of a hand-rolled lookalike check is that it works on the
examples its author thought of.

**Two things are deliberately not done here.**

No dependency is taken on a confusables data file. The full UTS #39 table maps
thousands of pairs, and the interesting targets here are ASCII: a ticker symbol
is impersonating ``USDC``, not impersonating Devanagari. :data:`TO_ASCII` covers
the characters that actually confuse with ASCII letters and digits, and
:func:`skeleton` says plainly that it is partial.

And nothing here decides anything. It reports what a string is made of. Whether
a token is a forgery is a question about contracts, not about characters, and
answering it here would put the conclusion in the wrong module.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

__all__ = [
    "TO_ASCII",
    "confusable",
    "is_mixed_script",
    "scripts",
    "skeleton",
    "suspicious_characters",
]


#: Characters that resemble an ASCII character, mapped to the one they resemble.
#:
#: A curated subset of UTS #39's confusables table, restricted to ASCII targets.
#: Provenance for each block, because a table like this is exactly the kind of
#: thing that acquires wrong entries once nobody remembers where it came from:
#:
#: * Cyrillic --- the block used by every impersonation token observed in the
#:   case this module was written for.
#: * Greek --- the other block with a large Latin-lookalike overlap.
#: * Fullwidth forms --- a mechanical transform of ASCII, so complete by
#:   construction.
#: * Mathematical alphanumerics --- ``𝐔𝐒𝐃𝐂`` renders as bold ASCII in most
#:   fonts and survives copy-paste.
#:
#: Digits that resemble letters (``0``/``O``, ``1``/``l``) are **not** here. They
#: are ordinary ASCII, both forms are legitimate in a ticker, and folding them
#: would make ``SOL`` and ``S0L`` indistinguishable in the wrong direction ---
#: turning a real symbol into a reported forgery.
TO_ASCII: dict[str, str] = {
    # Cyrillic
    "А": "A",
    "В": "B",
    "Е": "E",
    "К": "K",
    "М": "M",
    "Н": "H",
    "О": "O",
    "Р": "P",
    "С": "C",
    "Т": "T",
    "У": "Y",
    "Х": "X",
    "Ѕ": "S",
    "І": "I",
    "Ј": "J",
    "а": "a",
    "в": "b",
    "е": "e",
    "к": "k",
    "м": "m",
    "о": "o",
    "р": "p",
    "с": "c",
    "т": "t",
    "у": "y",
    "х": "x",
    "ѕ": "s",
    "і": "i",
    "ј": "j",
    "ԁ": "d",
    "һ": "h",
    "ӏ": "l",
    "ԛ": "q",
    "ԝ": "w",
    "г": "r",
    "п": "n",
    # Greek
    "Α": "A",
    "Β": "B",
    "Ε": "E",
    "Ζ": "Z",
    "Η": "H",
    "Ι": "I",
    "Κ": "K",
    "Μ": "M",
    "Ν": "N",
    "Ο": "O",
    "Ρ": "P",
    "Τ": "T",
    "Υ": "Y",
    "Χ": "X",
    "α": "a",
    "ο": "o",
    "ρ": "p",
    "ν": "v",
    "υ": "u",
    "κ": "k",
    "ι": "i",
    "τ": "t",
    "χ": "x",
    "γ": "y",
    # Armenian and Cherokee, both of which appear in wallet-drainer tickers
    "Ց": "S",
    "Օ": "O",
    "Ա": "U",
    "Ꭰ": "D",
    "Ꭼ": "E",
    "Ꮋ": "H",
    "Ꮑ": "N",
    "Ꭺ": "A",
    "Ꮯ": "C",
    "Ꮮ": "L",
    "Ꮲ": "P",
    "Ꭱ": "R",
    "Ꮪ": "S",
    "Ꭲ": "T",
    "Ꮷ": "J",
    "Ꮖ": "I",
    "Ꮶ": "K",
    "Ꮟ": "b",
    "Ᏼ": "B",
    "Ᏽ": "Y",
}

# Fullwidth forms and mathematical alphanumerics are mechanical ranges, so they
# are generated rather than typed --- typing them would introduce transcription
# errors into a table whose entire purpose is catching characters that look
# alike.
for _offset, _base in ((0xFF21, "A"), (0xFF41, "a")):
    for _i in range(26):
        TO_ASCII[chr(_offset + _i)] = chr(ord(_base) + _i)
for _i in range(10):
    TO_ASCII[chr(0xFF10 + _i)] = chr(ord("0") + _i)
for _start, _base in (
    (0x1D400, "A"),
    (0x1D41A, "a"),  # bold
    (0x1D434, "A"),
    (0x1D44E, "a"),  # italic
    (0x1D5A0, "A"),
    (0x1D5BA, "a"),  # sans-serif
    (0x1D670, "A"),
    (0x1D68A, "a"),  # monospace
):
    for _i in range(26):
        TO_ASCII[chr(_start + _i)] = chr(ord(_base) + _i)


@lru_cache(maxsize=4096)
def _script_of(ch: str) -> str:
    """The Unicode script a character belongs to.

    Read from the character's *name* rather than from a script table, because
    the standard library ships the name database and does not ship the script
    database. ``unicodedata.name('Ѕ')`` is ``'CYRILLIC CAPITAL LETTER DZE'``,
    and the first word is the script for every cased letter --- which is the
    only category a ticker symbol is made of.

    An unnamed character (unassigned, or a private-use codepoint) reports as
    ``UNKNOWN`` rather than raising. A symbol built from private-use characters
    is not a symbol anybody typed by accident, and losing that to an exception
    would be the wrong trade.
    """
    try:
        return unicodedata.name(ch).split()[0]
    except ValueError:
        return "UNKNOWN"


def scripts(text: str) -> set[str]:
    """The set of scripts the *letters* in ``text`` come from.

    Digits, spaces and punctuation are excluded: they are script-neutral in
    practice, and counting ``COMMON`` as a script would make every symbol
    containing a space or a hyphen look mixed.
    """
    # NFKC first. Reading the script from a character's *name* is a proxy, and
    # it breaks on compatibility glyphs: `U+212A KELVIN SIGN` reports as KELVIN
    # rather than LATIN, so `KUSDC` written with it looked single-script.
    # Normalising folds those to their Latin equivalents before the question is
    # asked. Mathematical alphanumerics still report as MATHEMATICAL, which is
    # correct --- and a wholly-mathematical symbol is caught by `skeleton`
    # instead, which is why there are three mechanisms rather than one.
    return {_script_of(ch) for ch in unicodedata.normalize("NFKC", text) if ch.isalpha()}


def is_mixed_script(text: str) -> bool:
    """Whether ``text`` draws its letters from more than one script.

    UTS #39 §5 calls a string that does this *not single-script*, and treats it
    as the strongest available signal of deliberate confusion --- because there
    is essentially no legitimate reason for one word to be half Latin and half
    Cyrillic, while there are many reasons to write a word entirely in either.

    This finds ``UЅDC``. It does not find ``ЕТН``, which is wholly Cyrillic and
    perfectly consistent; :func:`skeleton` is for that one.
    """
    return len(scripts(text)) > 1


def skeleton(text: str) -> str:
    """What ``text`` looks like, with every lookalike folded to ASCII.

    UTS #39 §4 calls this the *skeleton*: two strings are confusable when their
    skeletons are equal. Case is preserved --- ``usdc`` and ``USDC`` are not a
    forgery of each other, they are the same ticker written two ways, and
    folding case here would report every exchange's lowercase symbol as an
    impersonation of its own uppercase one.

    Compatibility decomposition runs first, so ``ⓤ`` and ``ｕ`` reduce before
    the table is consulted.

    **Partial by construction.** :data:`TO_ASCII` covers the blocks that
    impersonate ASCII in practice, not the whole confusables table. A skeleton
    that still contains non-ASCII is not evidence of innocence --- it means this
    function had nothing to say --- which is why
    :func:`suspicious_characters` reports what was left over instead of
    swallowing it.
    """
    decomposed = unicodedata.normalize("NFKC", text)
    return "".join(TO_ASCII.get(ch, ch) for ch in decomposed)


def confusable(left: str, right: str) -> bool:
    """Whether two strings would be read as the same string.

    Not whether they *are* the same: ``confusable("USDC", "USDC")`` is true and
    uninteresting. Callers comparing a candidate against a known-real symbol
    want the pair where the strings differ and the skeletons do not, and should
    test ``left != right and confusable(left, right)``.
    """
    return skeleton(left) == skeleton(right)


def suspicious_characters(text: str) -> list[tuple[str, str, str]]:
    """Every character that is not plain ASCII, with its codepoint and name.

    Returned so that a report can *show its work*. "This symbol is a forgery" is
    an assertion; "position 1 is U+0405 CYRILLIC CAPITAL LETTER DZE, which
    resembles S" is something the reader can check, and being checkable is the
    difference between a finding and an accusation.

    Emoji and other non-letter characters are included. A ticker containing one
    is not a homoglyph attack, but it is not an accident either.
    """
    found: list[tuple[str, str, str]] = []
    for ch in text:
        if ord(ch) < 128:
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "unnamed codepoint"
        found.append((ch, f"U+{ord(ch):04X}", name))
    return found
