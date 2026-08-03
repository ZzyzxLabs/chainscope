"""Reading GraphSense TagPacks without losing what makes them worth reading.

Their confidence is keyed on *how* a tag was obtained --- `ownership` (the
creator holds the key) against `web_crawl` --- on a 0-100 scale. This package's
ladder has five steps, so the mapping loses resolution. What it must not lose
is the original: a reader has to be able to see that a HIGH here came from
`authority_data` at 60 and not from `service_api` at 70.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from chainscope.attribution.sources.tagpack import (  # noqa: E402
    _CONFIDENCE,
    TagPackSource,
)
from chainscope.core.attribution import Confidence  # noqa: E402


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    root = tmp_path / "tagpacks"
    root.mkdir()
    (root / "one.yaml").write_text(
        "creator: INTERPOL CNTL\n"
        "title: INTERPOL TagPack\n"
        "confidence: authority_data\n"
        "lastmod: 2021-11-12\n"
        "source: https://www.interpol.int\n"
        "tags:\n"
        "- address: 3QyUSB4eRYePHcvpS6k6YDMBUDGXRSSMPc\n"
        "  label: bitcoinbon.at\n"
        "  currency: BTC\n"
        "  category: exchange\n"
        "  actor: bitcoinbon\n"
        "- address: '0xAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaa'\n"
        "  label: someone\n"
        "  currency: ETH\n"
        "  category: mixing_service\n"
        "  confidence: web_crawl\n"
    )
    (root / "broken.yaml").write_text("this: [is not\n  valid: yaml\n")
    return root


def test_header_fields_reach_every_tag(packs: Path) -> None:
    """A 50,000-address pack states creator and confidence once, at the top."""
    found = TagPackSource(packs).lookup("3QyUSB4eRYePHcvpS6k6YDMBUDGXRSSMPc")
    assert len(found) == 1
    assert "INTERPOL" in found[0].source


def test_a_tag_overrides_its_pack(packs: Path) -> None:
    """The per-tag `confidence` wins over the header's."""
    found = TagPackSource(packs).lookup("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert found and found[0].confidence == Confidence.LOW  # web_crawl, not authority_data


def test_the_original_confidence_survives_the_mapping(packs: Path) -> None:
    """Five steps cannot hold a hundred levels, so the id travels with it."""
    found = TagPackSource(packs).lookup("3QyUSB4eRYePHcvpS6k6YDMBUDGXRSSMPc")
    assert "authority_data" in found[0].rationale
    assert "60/100" in found[0].rationale


def test_a_sanctions_style_source_is_not_promoted_to_certain() -> None:
    """Their judgement, kept.

    A designation is an authoritative claim about an entity; the address-to-
    entity mapping inside it is still research. Only `ownership` and
    `ledger_immanent` --- holding the key, or reading the ledger --- earn CERTAIN.
    """
    assert _CONFIDENCE["authority_data"][1] is not Confidence.CERTAIN
    assert _CONFIDENCE["ownership"][1] is Confidence.CERTAIN
    assert _CONFIDENCE["ledger_immanent"][1] is Confidence.CERTAIN


def test_evm_addresses_fold_and_base58_does_not(packs: Path) -> None:
    """The rule every source here follows."""
    source = TagPackSource(packs)
    assert source.lookup("0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert not source.lookup("3qyusb4erycephcvps6k6ydmbudgxrssmpc")


def test_one_broken_pack_does_not_cost_the_others(packs: Path) -> None:
    """Seventy-seven files; one bad one must not empty the corpus."""
    assert TagPackSource(packs).lookup("3QyUSB4eRYePHcvpS6k6YDMBUDGXRSSMPc")


def test_a_missing_checkout_is_reported_not_answered_as_empty(tmp_path: Path) -> None:
    """ "No tags" and "no corpus" are different claims."""
    from chainscope.attribution.base import SourceError

    source = TagPackSource(tmp_path / "absent")
    assert source.ready() is False
    with pytest.raises(SourceError, match="tagpacks"):
        source.lookup("3QyUSB4eRYePHcvpS6k6YDMBUDGXRSSMPc")


def test_an_unknown_currency_yields_no_chain(tmp_path: Path) -> None:
    """Rather than a guessed one: a tag on the wrong chain asserts something
    about twenty bytes on a network nobody looked at."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "x.yaml").write_text(
        "creator: c\ntitle: t\nconfidence: heuristic\ntags:\n"
        "- address: XYZ123\n  label: l\n  currency: NOTACHAIN\n"
    )
    found = TagPackSource(root).lookup("XYZ123")
    assert found and found[0].chain is None
