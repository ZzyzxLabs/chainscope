"""Tokens that pretend to be other tokens.

Every symbol in `OBSERVED` was taken from one real case: a seed address with 55
ERC-20 transfers, of which 42 belonged to assets impersonating USDC or ETH. Six
were genuine, and those six were the answer.

They are used as fixtures rather than invented ones because an invented forgery
is a forgery its author already knew how to catch. Three distinct mechanisms
appear in that data and no single check finds all three:

* ``UЅDC`` --- Latin with one Cyrillic letter spliced in. Mixed-script.
* ``ЕТН``  --- wholly Cyrillic, therefore *not* mixed, and consistent.
* ``ETH``  --- plain ASCII, a token simply named after a real one. Unicode has
  nothing to say; only the contract address does.

The third is why this is not a Unicode problem with a Unicode answer. `symbol()`
is a string the deployer chose. The check that decides is contract identity, and
the tests below are arranged so that removing any one of the three mechanisms
leaves failures behind.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from chainscope.analysis.impersonation import (
    CANONICAL,
    Verdict,
    canonical_for,
    inspect_assets,
    report,
)
from chainscope.core.chainid import BSC, ETHEREUM
from chainscope.core.confusable import (
    confusable,
    is_mixed_script,
    scripts,
    skeleton,
    suspicious_characters,
)

REAL_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
REAL_USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"

#: ``(contract, symbol, transfer count)`` --- the distinct assets in that case.
OBSERVED = [
    ("0xa599e8c7f4bac6512e250055a96a20a72bbac75e", "UЅDC", 12),
    ("0x52f7d663598a0b4277b02c4fc0b83cab05615d2c", "EТH", 11),
    ("0x1890b18df4922855130bba2678f2d7c1ed650951", "ЕТН...", 9),
    ("0x2abcfd11b4dd7af003738b84ed25b25c7f20d494", "ЕТН...", 6),
    (REAL_USDT, "USDT", 4),
    (REAL_USDC, "USDC", 2),
    ("0x33a1ada445dfd27af86ef6b2402d9d02e356cf69", "TESLA AI", 2),
    ("0xcb702b68b7934d48c1e3ea86f10a238f53aeb1bc", "NVIDIA AI", 2),
    ("0xa4a78fe7c6b3925e9047b7a485013fe7f6fbc6a6", "ETH", 2),
    ("0xc330fd8bd9ce107aa2f2708d9015385903aa4684", "EТH", 2),
    ("0xb5eff4ac0e907211b7876a54cfeb136af540b403", "BERRY AI", 1),
    ("0x58c4f482fdde2e94055eebf476acfccbe3afe809", "VISTA TRUMP", 1),
    ("0x2b591e99afe9f32eaa6214f7b7629768c40eeb39", "HEX", 1),
]


def _transfers(
    rows: list[tuple[str, str, int]] | None = None, chain: object = ETHEREUM
) -> list:
    out = []
    for contract, symbol, count in rows if rows is not None else OBSERVED:
        for _ in range(count):
            out.append(
                SimpleNamespace(
                    chain=chain,
                    asset=SimpleNamespace(raw=contract, key=contract.lower()),
                    amount=SimpleNamespace(symbol=symbol, decimals=18),
                )
            )
    return out


class TestTheUnicodeLayer:
    """UTS #39's two mechanisms, checked separately so a gap is visible."""

    def test_mixed_script_finds_the_spliced_letter(self) -> None:
        assert is_mixed_script("UЅDC")
        assert scripts("UЅDC") == {"LATIN", "CYRILLIC"}

    def test_mixed_script_does_not_fire_on_a_real_symbol(self) -> None:
        for symbol in ("USDC", "USDT", "WETH", "TESLA AI", "HEX"):
            assert not is_mixed_script(symbol), symbol

    def test_mixed_script_alone_misses_the_wholly_cyrillic_one(self) -> None:
        """The gap that makes the second mechanism necessary.

        ``ЕТН`` is entirely Cyrillic and therefore perfectly consistent. A
        checker built only on mixed-script would pass it.
        """
        assert not is_mixed_script("ЕТН")

    def test_the_skeleton_catches_what_mixed_script_missed(self) -> None:
        assert skeleton("ЕТН") == "ETH"
        assert confusable("ЕТН", "ETH")

    def test_case_is_not_folded(self) -> None:
        # `usdc` and `USDC` are the same ticker written two ways, not a forgery
        # of each other. Folding case here would report every exchange's
        # lowercase symbol as impersonating its own uppercase one.
        assert not confusable("usdc", "USDC")

    def test_digits_that_look_like_letters_are_left_alone(self) -> None:
        # Both are ordinary ASCII and both are legitimate in a ticker. Folding
        # them would turn a real symbol into a reported forgery.
        assert not confusable("S0L", "SOL")

    def test_it_shows_its_work(self) -> None:
        found = suspicious_characters("UЅDC")
        assert found == [("Ѕ", "U+0405", "CYRILLIC CAPITAL LETTER DZE")]

    def test_an_ascii_symbol_has_nothing_to_show(self) -> None:
        assert suspicious_characters("USDC") == []

    @pytest.mark.parametrize(
        ("fake", "real"),
        [("ＵＳＤＣ", "USDC"), ("𝐔𝐒𝐃𝐂", "USDC"), ("ЅОL", "SOL")],
    )
    def test_the_other_blocks_fold_too(self, fake: str, real: str) -> None:
        # Fullwidth and mathematical alphanumerics both survive copy-paste and
        # render as ordinary letters.
        assert fake != real
        assert confusable(fake, real)


class TestTheCanonicalRegistry:
    """The check Unicode cannot make."""

    def test_a_real_contract_is_genuine(self) -> None:
        found = {a.contract: a for a in inspect_assets(_transfers([(REAL_USDC, "USDC", 1)]))}
        assert found[REAL_USDC].verdict == Verdict.GENUINE

    def test_an_ascii_impostor_is_caught(self) -> None:
        """The case no amount of Unicode analysis finds.

        A contract with the symbol ``ETH``: pure ASCII, single script, no
        confusable characters. It is a forgery because ETH on an EVM chain is
        native and has no contract at all.
        """
        fake = "0xa4a78fe7c6b3925e9047b7a485013fe7f6fbc6a6"
        found = {a.contract: a for a in inspect_assets(_transfers([(fake, "ETH", 1)]))}
        assert found[fake].verdict == Verdict.FORGED
        assert not is_mixed_script("ETH")
        assert suspicious_characters("ETH") == []

    def test_a_symbol_from_the_wrong_contract_is_forged(self) -> None:
        impostor = "0x" + "9" * 40
        found = {a.contract: a for a in inspect_assets(_transfers([(impostor, "USDC", 1)]))}
        assert found[impostor].verdict == Verdict.FORGED

    def test_the_same_symbol_on_another_chain_is_a_different_contract(self) -> None:
        # USDC on BSC is not USDC on Ethereum, and the mainnet address on BSC is
        # not the real one there.
        found = inspect_assets(_transfers([(REAL_USDC, "USDC", 1)], chain=BSC), BSC)
        assert found[0].verdict == Verdict.FORGED

    def test_absence_from_the_registry_is_not_a_verdict(self) -> None:
        """The error that would invert this module's purpose.

        Most tokens are in no registry and are entirely real. Reporting them as
        suspicious would make the report useless and would train the reader to
        ignore the section.
        """
        found = inspect_assets(_transfers([("0x" + "7" * 40, "TESLA AI", 1)]))
        assert found[0].verdict == Verdict.UNLISTED
        assert not found[0].is_impersonation

    def test_none_and_empty_mean_different_things(self) -> None:
        # `None` is "no opinion". An empty set is "native asset, so any contract
        # claiming this symbol is impersonating". Collapsing them loses the
        # ASCII case entirely.
        assert canonical_for(ETHEREUM, "NOSUCHTOKEN") is None
        assert canonical_for(ETHEREUM, "ETH") == frozenset()

    def test_the_registry_holds_lowercase_hex(self) -> None:
        # The lookup folds the incoming contract; entries that were checksummed
        # would never match and would report the real USDC as a forgery.
        for contracts in CANONICAL.values():
            for contract in contracts:
                assert contract == contract.lower()


class TestTheRealCase:
    """The whole observed set, end to end."""

    def test_the_share_is_what_was_measured(self) -> None:
        rep = report(_transfers(), ETHEREUM)
        assert rep.total_transfers == 55
        assert rep.forged_transfers == 42

    def test_the_genuine_rows_are_the_six_that_mattered(self) -> None:
        rep = report(_transfers(), ETHEREUM)
        genuine = {a.symbol: a.transfers for a in rep.assets if a.verdict == Verdict.GENUINE}
        assert genuine == {"USDT": 4, "USDC": 2}

    def test_the_summary_states_the_scale(self) -> None:
        # "Three forged tokens" is mildly interesting. "42 of 55 transfers" is
        # the sentence that changes what somebody does next.
        summary = report(_transfers(), ETHEREUM).summary()
        assert "42 of 55" in summary
        assert "76%" in summary

    def test_two_contracts_sharing_a_symbol_stay_apart(self) -> None:
        """``EТH`` appears twice, from two different contracts.

        Any grouping on the symbol string merges them --- which is exactly what
        the forger is buying.
        """
        rep = report(_transfers(), ETHEREUM)
        eth_like = [a for a in rep.assets if a.symbol == "EТH"]
        assert len(eth_like) == 2
        assert len({a.contract for a in eth_like}) == 2

    def test_impersonations_are_ordered_by_how_much_they_explain(self) -> None:
        # A forgery responsible for 12 of 55 rows is a different problem from
        # one responsible for a single dust transfer.
        rep = report(_transfers(), ETHEREUM)
        counts = [a.transfers for a in rep.impersonations]
        assert counts == sorted(counts, reverse=True)

    def test_nothing_is_dropped(self) -> None:
        # It reports; it does not filter. A tool that silently removed what it
        # judged fake would be making the same unreviewable decision as one that
        # silently kept it, and would be harder to argue with.
        rep = report(_transfers(), ETHEREUM)
        assert sum(a.transfers for a in rep.assets) == 55

    def test_every_asset_is_accounted_for(self) -> None:
        rep = report(_transfers(), ETHEREUM)
        assert len(rep.assets) == len(OBSERVED)


class TestTheResult:
    def test_a_forgery_outranks_the_number_it_invalidates(self) -> None:
        from chainscope.analysis.impersonation import analyse
        from chainscope.core.result import Severity

        result = analyse(_transfers(), ETHEREUM)
        forged = [f for f in result.findings if f.severity == Severity.CRITICAL]
        assert forged, "an impersonation is not a detail; it says another number is wrong"

    def test_the_genuine_assets_get_a_finding_too(self) -> None:
        from chainscope.analysis.impersonation import analyse

        result = analyse(_transfers(), ETHEREUM)
        titles = " ".join(f.title for f in result.findings)
        # The more useful half: an investigator who reads only "these are fake"
        # still has to work out which rows to trust.
        assert "confirmed against a canonical contract" in titles

    def test_the_warning_says_the_totals_are_unsafe(self) -> None:
        from chainscope.analysis.impersonation import analyse

        result = analyse(_transfers(), ETHEREUM)
        assert any("grouped by symbol is unsafe" in w for w in result.warnings)

    def test_a_clean_set_produces_no_warning(self) -> None:
        # A warning that fires every run is one people stop reading.
        from chainscope.analysis.impersonation import analyse

        result = analyse(_transfers([(REAL_USDC, "USDC", 3)]), ETHEREUM)
        assert result.warnings == ()

    def test_a_clean_set_still_says_what_it_did_not_check(self) -> None:
        rep = report(_transfers([("0x" + "3" * 40, "SOMETOKEN", 1)]), ETHEREUM)
        assert "most tokens are in no registry" in rep.summary().lower()
