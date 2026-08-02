"""Renderers, case bundles, and the CLI.

The renderer tests all circle one requirement: a weak claim must not be able to
reach a reader looking like a strong one. That is the product, and a renderer
that tidies it away has removed the reason the project exists.
"""

import json
from datetime import datetime, timezone

import pytest

from chainscope.case.bundle import Bundle, BundleError
from chainscope.cli.main import main
from chainscope.core.attribution import (
    Attribution,
    Category,
    Confidence,
    Method,
    merge,
)
from chainscope.core.chainid import ETHEREUM
from chainscope.core.hypothesis import Hypothesis, ScoreFactor
from chainscope.core.result import Evidence, Finding, Result, Severity
from chainscope.render.base import qualify, qualify_entity
from chainscope.render.jsonout import JsonRenderer
from chainscope.render.markdown import MarkdownRenderer
from chainscope.render.terminal import TerminalRenderer

ADDR = "0x28c6c06298d514db089934071355e5743bf21d60"


def claim(confidence, **kw):
    base = dict(
        address=ADDR,
        chain=ETHEREUM,
        label="Acme Exchange",
        category=Category.CEX,
        confidence=confidence,
        method=Method.LABEL,
        source="test@2026-01-01",
    )
    if confidence <= Confidence.LOW:
        base["rationale"] = "payout timing and fee rate are consistent"
        base["method"] = Method.INFERENCE
    return Attribution(**{**base, **kw})


def sample_result(**kw) -> Result:
    defaults = dict(
        analyzer="demo",
        findings=(
            Finding(
                title="a thing was found",
                severity=Severity.NOTABLE,
                detail="Some explanation of the thing.",
                data={"count": 3, "addresses": ["a", "b", "c", "d"]},
            ),
        ),
        params={"address": ADDR},
        evidence=Evidence(query_keys=("k1", "k2")),
        finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return Result(**{**defaults, **kw})


class TestQualification:
    def test_strong_claims_read_as_labels(self):
        assert qualify(claim(Confidence.HIGH)) == "Acme Exchange"
        assert qualify(claim(Confidence.CERTAIN)) == "Acme Exchange"

    @pytest.mark.parametrize(
        ("level", "word"),
        [
            (Confidence.MEDIUM, "probably"),
            (Confidence.LOW, "possibly"),
            (Confidence.SPECULATIVE, "speculatively"),
        ],
    )
    def test_weak_claims_are_hedged_in_plain_words(self, level, word):
        # "possibly" is understood; "MEDIUM confidence" invites rounding up.
        text = qualify(claim(level))
        assert text.startswith(word)

    def test_weak_claims_carry_their_basis(self):
        text = qualify(claim(Confidence.LOW))
        assert "payout timing and fee rate" in text

    def test_disagreement_is_surfaced(self):
        entity = merge(
            [
                claim(Confidence.HIGH),
                claim(Confidence.HIGH, category=Category.MIXER, source="other"),
            ]
        )
        assert "sources disagree" in qualify_entity(entity)

    def test_unknown_entity(self):
        assert qualify_entity(None) == "unknown"


class TestTerminalRenderer:
    def test_warnings_come_before_findings(self):
        """A reader who meets the caveats last has already concluded."""
        out = TerminalRenderer(colour=False).render(
            sample_result(warnings=("results were truncated",))
        )
        assert out.index("truncated") < out.index("a thing was found")

    def test_contested_hypothesis_is_marked(self):
        h = Hypothesis(
            claim="A is the payout",
            factors=(ScoreFactor("f", 5.0, True),),
            confidence=Confidence.LOW,
            alternatives=(
                Hypothesis(
                    claim="B is the payout",
                    factors=(ScoreFactor("f", 4.5, True),),
                    confidence=Confidence.LOW,
                ),
            ),
        )
        out = TerminalRenderer(colour=False).render(sample_result(hypotheses=(h,), findings=()))
        assert "contested" in out and "not decisive" in out

    def test_hypotheses_are_labelled_as_inference(self):
        h = Hypothesis(claim="x", confidence=Confidence.LOW)
        out = TerminalRenderer(colour=False).render(sample_result(hypotheses=(h,)))
        assert "not observation" in out

    def test_empty_result_says_so(self):
        out = TerminalRenderer(colour=False).render(Result(analyzer="demo"))
        assert "no findings" in out

    def test_colour_can_be_disabled(self):
        assert "\033[" not in TerminalRenderer(colour=False).render(sample_result())


class TestMarkdownRenderer:
    def test_warnings_are_a_leading_blockquote(self):
        out = MarkdownRenderer().render(sample_result(warnings=("truncated",)))
        assert "> **Read these first.**" in out
        assert out.index("truncated") < out.index("a thing was found")

    def test_score_factors_are_tabulated(self):
        """So the reasoning can be checked rather than taken on trust."""
        h = Hypothesis(
            claim="x",
            factors=(ScoreFactor("payer_is_service", 5.0, True, "30k txs"),),
            confidence=Confidence.LOW,
        )
        out = MarkdownRenderer().render(sample_result(hypotheses=(h,)))
        assert "| `payer_is_service` | +5 | 30k txs |" in out

    def test_reproducibility_section_records_params(self):
        out = MarkdownRenderer().render(sample_result())
        assert "## Reproducibility" in out
        assert ADDR in out

    def test_query_count_is_reported(self):
        out = MarkdownRenderer().render(sample_result())
        assert "2 queries recorded" in out


class TestJsonRenderer:
    def test_round_trips(self):
        data = json.loads(JsonRenderer().render(sample_result()))
        assert data["analyzer"] == "demo"
        assert data["findings"][0]["title"] == "a thing was found"

    def test_reliability_flag_is_top_level(self):
        """A consumer reading `findings` and ignoring `warnings` would treat a
        truncated search as a complete one."""
        clean = json.loads(JsonRenderer().render(sample_result()))
        flagged = json.loads(JsonRenderer().render(sample_result(warnings=("truncated",))))
        assert clean["reliable"] is True
        assert flagged["reliable"] is False

    def test_enums_serialise_to_values(self):
        data = json.loads(JsonRenderer().render(sample_result()))
        assert data["findings"][0]["severity"] == "notable"


class TestBundle:
    def test_create_and_reopen(self, tmp_path):
        b = Bundle.create(tmp_path / "case", title="Case 1", subject=ADDR)
        b.add_result(sample_result())
        reopened = Bundle.open(tmp_path / "case")
        assert reopened.title == "Case 1"
        assert reopened.summary()["analyses"] == 1

    def test_results_are_readable_back(self, tmp_path):
        b = Bundle.create(tmp_path / "case")
        b.add_result(sample_result())
        assert b.read_result(0)["analyzer"] == "demo"

    def test_unreplayable_without_a_cache(self, tmp_path):
        b = Bundle.create(tmp_path / "case")
        assert not b.replayable

    def test_replayable_once_the_cache_is_attached(self, tmp_path):
        from chainscope.transport.cache import Cache, Volatility

        cache = Cache(tmp_path / "q.sqlite")
        cache.put("k", {"v": 1}, Volatility.IMMUTABLE)
        b = Bundle.create(tmp_path / "case")
        b.attach_cache(cache)
        assert b.replayable
        assert b.replay_cache().get("k", Volatility.IMMUTABLE) == {"v": 1}

    def test_archive_produces_a_zip(self, tmp_path):
        b = Bundle.create(tmp_path / "case", title="x")
        b.add_result(sample_result())
        dest = b.archive(tmp_path / "case.zip")
        assert dest.exists() and dest.stat().st_size > 0

    def test_missing_manifest_is_rejected(self, tmp_path):
        (tmp_path / "notabundle").mkdir()
        with pytest.raises(BundleError, match="not a bundle"):
            Bundle.open(tmp_path / "notabundle")

    def test_future_manifest_version_is_rejected(self, tmp_path):
        d = tmp_path / "case"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"manifest_version": 99}))
        with pytest.raises(BundleError, match="version 99"):
            Bundle.open(d)

    def test_malformed_manifest_is_rejected(self, tmp_path):
        """Bundles arrive from other people; treat them as untrusted."""
        d = tmp_path / "case"
        d.mkdir()
        (d / "manifest.json").write_text("{not json")
        with pytest.raises(BundleError, match="not valid JSON"):
            Bundle.open(d)


class TestCli:
    def test_analyzer_list(self, capsys):
        assert main(["analyze", "--list"]) == 0
        assert "consolidation" in capsys.readouterr().out

    def test_unknown_analyzer(self, capsys):
        assert main(["analyze", "nope"]) == 2
        assert "unknown analyzer" in capsys.readouterr().out

    def test_doctor_fails_when_the_central_question_is_unanswerable(
        self, capsys, monkeypatch, tmp_path
    ):
        """Exit code, not just output.

        `doctor` reports on the environment, so a test that does not control
        the environment passes or fails depending on whose machine it runs on
        --- and this one did: with a key in .env it exited 0, and on CI with
        none it would have exited 1. Both are pinned here explicitly.
        """
        monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
        # An empty directory, so the walk-up .env search finds nothing.
        monkeypatch.chdir(tmp_path)

        assert main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "ADDRESS_HISTORY" in out
        assert "unreachable  ADDRESS_HISTORY" in out
        assert "ETHERSCAN_API_KEY" in out

    def test_doctor_passes_once_the_key_is_present(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("ETHERSCAN_API_KEY", "a-key-long-enough-to-register")
        monkeypatch.chdir(tmp_path)

        assert main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "unreachable  ADDRESS_HISTORY" not in out

    def test_doctor_is_chain_aware(self, capsys, monkeypatch, tmp_path):
        """Sui offers ADDRESS_HISTORY without a key, and that says nothing
        about whether the question is answerable on Ethereum. Reporting the
        capability as reachable for every chain was exactly the "wrong but
        plausible" answer this command exists to prevent."""
        monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        assert main(["doctor", "--chain", "eth"]) == 1
        assert "unreachable  ADDRESS_HISTORY" in capsys.readouterr().out

        assert main(["doctor", "--chain", "sui:mainnet"]) == 0
        assert "unreachable  ADDRESS_HISTORY" not in capsys.readouterr().out

    def test_doctor_lists_discovered_plugins(self, capsys, monkeypatch, tmp_path):
        """It used to print "No providers configured in this build" as a
        hardcoded string, to everybody, regardless of what they had."""
        monkeypatch.chdir(tmp_path)
        main(["doctor"])
        out = capsys.readouterr().out
        assert "plugins" in out
        assert "etherscan" in out
        assert "sui" in out

    def test_label_without_sources_is_an_error(self, capsys):
        assert main(["label", ADDR]) == 2
        assert "no sources configured" in capsys.readouterr().out

    def test_label_exit_code_flags_an_unreliable_lookup(self, tmp_path, capsys):
        """Non-zero on 'could not check', so a pipeline does not read it as a pass."""
        assert main(["label", ADDR, "--sanctions", str(tmp_path / "absent.json")]) == 1
        assert "UNKNOWN" in capsys.readouterr().out

    def test_label_clean_address_exits_zero(self, tmp_path, capsys):
        sdn = tmp_path / "ofac.json"
        sdn.write_text(json.dumps({"fetched": "2026-01-01T00:00:00Z", "addresses": {}}))
        assert main(["label", ADDR, "--sanctions", str(sdn)]) == 0
        assert "sanctioned: no" in capsys.readouterr().out

    def test_bundle_inspection(self, tmp_path, capsys):
        b = Bundle.create(tmp_path / "case", title="Case 1")
        b.add_result(sample_result(warnings=("truncated",)))
        # Exit 1: not replayable, so the findings cannot be independently checked.
        assert main(["bundle", str(tmp_path / "case")]) == 1
        out = capsys.readouterr().out
        assert "Case 1" in out
        assert "cannot be independently verified" in out
