"""A ticker can render as USDC and not be USDC, with no visible difference.

Three counterfeit tokens from the LOOPSDAO / LpdFi exploit (BSC, 2 August
2026). The attacker's laundering path was surrounded by address-poisoning
traffic, and the tokens carrying it had these symbols:

===============================================  ==============================
``ÚЅDC``   U+00DA, U+0405, D, C                   Latin U-with-acute, Cyrillic Ѕ
``U឵S឵Dꓚ`` U, U+17B5, S, U+17B5, D, U+A4DA        invisible marks, Lisu CA
``B឵N឵B``  B, U+17B5, N, U+17B5, B                invisible marks only
===============================================  ==============================

Every one of them displays as an ordinary ticker. `skeleton` normalised with
NFKC, which **preserves** both the acute on ``Ú`` and the U+17B5 non-spacing
marks, so all three skeletons kept their disguise, the comparison against
``USDC`` failed, and the classifier filed deliberate forgeries as
``unknown-script`` with an empty explanation. The page then showed them under
"no canonical entry for this symbol; nothing is claimed" --- true, and the
opposite of the useful statement, which is that the symbol exists to be read as
USDC.

The fix is NFKD plus dropping zero-width categories. The tests below hold both
halves: that the disguises fold, and that the folding did not become so
aggressive that a legitimately distinct symbol now reads as a forgery.
"""

from __future__ import annotations

import pytest

from chainscope.analysis.impersonation import _classify, load_canonical
from chainscope.core.chainid import ChainId
from chainscope.core.confusable import confusable, skeleton, suspicious_characters

BSC = ChainId.evm(56)
CANONICAL_USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"

#: contract, symbol as published, what it imitates
OBSERVED = [
    ("0x34a7cc385dccb0f034c49b9a2fc8d0c747705e2f", "ÚЅDC", "USDC"),
    ("0x2ad49e9775e1f63bb29e366bcbe5432f8c9cc81e", "U឵S឵Dꓚ", "USDC"),
    ("0xf6de554678a8dae628b3d2c4ea021723e3e8d898", "B឵N឵B", "BNB"),
]


@pytest.fixture(scope="module", autouse=True)
def canonical() -> None:
    load_canonical()


@pytest.mark.parametrize(("contract", "symbol", "imitates"), OBSERVED)
def test_the_disguise_folds_away(contract: str, symbol: str, imitates: str) -> None:
    assert skeleton(symbol) == imitates
    assert confusable(symbol, imitates)


@pytest.mark.parametrize(("contract", "symbol", "imitates"), OBSERVED)
def test_it_is_called_a_lookalike_and_told_what_it_imitates(
    contract: str, symbol: str, imitates: str
) -> None:
    verdict, resembles, why = _classify(BSC, contract, symbol)
    assert verdict == "lookalike"
    assert resembles == imitates
    # The reader has to be able to check the claim, not just receive it.
    assert any("renders identically" in line for line in why)


def test_an_invisible_character_is_reported_as_present() -> None:
    """The evidence is a character nobody can see. It has to be named."""
    found = suspicious_characters("U឵S឵Dꓚ")
    codepoints = {entry[1] for entry in found}
    assert "U+17B5" in codepoints
    assert "U+A4DA" in codepoints


def test_the_real_token_is_still_the_real_token() -> None:
    verdict, _, why = _classify(BSC, CANONICAL_USDC, "USDC")
    assert verdict == "genuine"
    assert any("canonical" in line for line in why)


# --------------------------------------------------------------- not too eager
#
# Dropping marks and folding a whole extra Unicode block widens what counts as
# confusable, and the cost of widening it too far is an accusation against a
# real token. These are the cases that must NOT move.


@pytest.mark.parametrize("pair", [("SOL", "S0L"), ("USDC", "USDT"), ("BNB", "BUSD")])
def test_distinct_ascii_symbols_stay_distinct(pair: tuple[str, str]) -> None:
    """Zero and O are both legitimate in a ticker; folding them accuses SOL."""
    assert not confusable(*pair)


def test_case_is_not_a_forgery() -> None:
    """`usdc` and `USDC` are one ticker written two ways."""
    assert not confusable("usdc", "USDC")


def test_rotated_lisu_letters_are_not_folded() -> None:
    """`ꓘ` is a mirrored K. A reader sees the difference, so the tool must not
    claim they render identically."""
    assert skeleton("ꓘ") == "ꓘ"
    assert not confusable("ꓘUSD", "KUSD")


def test_a_symbol_with_a_legitimate_accent_still_reports_its_characters() -> None:
    """Folding the mark is right for comparison and must not hide it from the
    report --- the whole argument for the mark being suspicious is that it is
    there and invisible."""
    assert skeleton("CAFÉ") == "CAFE"
    assert any(entry[1] == "U+00C9" for entry in suspicious_characters("CAFÉ"))
