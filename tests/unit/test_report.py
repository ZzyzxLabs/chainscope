"""What a report must say, and what it must not leave out."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from chainscope.case.log import CaseLog, Note, NoteKind
from chainscope.cli.commands import report
from chainscope.core.attribution import Attribution, Category, Confidence, Method
from chainscope.render.terminal import TerminalRenderer
from chainscope.store.sqlite import SqliteStore

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def args_for(tmp_path: Path, **kw: object) -> argparse.Namespace:
    """Parse real arguments, then point the paths at a temp directory.

    Built through `report.add_parser` rather than hand-assembled: a namespace
    typed out here agrees with whatever the test author remembered, so renaming
    a flag or adding one with a default would leave every test below passing
    against a command that no longer exists in that shape.
    """
    parser = argparse.ArgumentParser()
    report.add_parser(parser.add_subparsers(dest="command"), "report")
    args = parser.parse_args(["report", "--title", "Test case"])
    args.store = tmp_path / "store.db"
    args.case = tmp_path / "case.db"
    args.attestation = tmp_path / "attestation.json"
    args.out = tmp_path / "report.md"
    for key, value in kw.items():
        if not hasattr(args, key):
            raise AssertionError(f"report has no --{key.replace('_', '-')} option")
        setattr(args, key, value)
    return args


def seed_notes(tmp_path: Path) -> CaseLog:
    log = CaseLog(tmp_path / "case.db")
    log.add(
        Note(
            at=NOW,
            analyst="alice@lab",
            identified_by="env",
            kind=NoteKind.OBSERVATION,
            body="eleven fresh addresses were funded",
        )
    )
    return log


def claim(**kw: object) -> Attribution:
    fields: dict[str, object] = {
        "address": "0xaaa",
        "chain": None,
        "label": "Exchange hot wallet",
        "category": Category.CEX,
        "confidence": Confidence.HIGH,
        "method": Method.LABEL,
        "source": "explorer tag",
        "analyst": "alice@lab",
    }
    fields.update(kw)
    return Attribution(**fields)  # type: ignore[arg-type]


class TestEmptyCase:
    def test_refuses_rather_than_writing_an_empty_report(self, tmp_path: Path) -> None:
        # An empty report is worse than none: it looks like a case with no
        # findings rather than a case nobody has written anything about.
        args = args_for(tmp_path)
        assert report.run(args, TerminalRenderer()) == 2
        assert not args.out.exists()


class TestNarrative:
    def test_carries_the_author_of_every_note(self, tmp_path: Path) -> None:
        seed_notes(tmp_path).close()
        args = args_for(tmp_path)
        assert report.run(args, TerminalRenderer()) == 0
        assert "alice@lab" in args.out.read_text()

    def test_open_questions_come_before_the_narrative(self, tmp_path: Path) -> None:
        log = seed_notes(tmp_path)
        log.add(
            Note(
                at=NOW,
                analyst="alice@lab",
                identified_by="env",
                kind=NoteKind.QUESTION,
                body="who funded the gas?",
            )
        )
        log.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert text.index("Not yet known") < text.index("## Narrative")

    def test_superseded_notes_are_shown_and_marked(self, tmp_path: Path) -> None:
        log = seed_notes(tmp_path)
        log.add(
            Note(
                at=NOW,
                analyst="alice@lab",
                identified_by="env",
                kind=NoteKind.CORRECTION,
                body="only nine were fresh",
                supersedes=1,
            )
        )
        log.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "eleven fresh addresses were funded" in text
        assert "superseded" in text
        assert "Replaces note 1" in text

    def test_says_so_when_nothing_was_written_down(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "store.db")
        store.put_attributions([claim()])
        store.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "reasoning behind this case is not recorded" in args.out.read_text()


class TestContributors:
    def test_counts_somebody_who_only_made_claims(self, tmp_path: Path) -> None:
        # The colleague who labelled thirty addresses and wrote no narrative is
        # the person most likely to be asked about a disagreement.
        seed_notes(tmp_path).close()
        store = SqliteStore(tmp_path / "store.db")
        store.put_attributions([claim(analyst="bob@lab")])
        store.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "bob@lab" in text
        assert "1 claim(s)" in text

    def test_flags_an_unverified_os_account(self, tmp_path: Path) -> None:
        log = CaseLog(tmp_path / "case.db")
        log.add(
            Note(
                at=NOW,
                analyst="laptop",
                identified_by="os",
                kind=NoteKind.OBSERVATION,
                body="a machine name is not authorship",
            )
        )
        log.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "OS account, unverified" in args.out.read_text()


class TestDisagreement:
    def test_names_both_analysts_and_picks_neither(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "store.db")
        store.put_attributions(
            [
                claim(label="Exchange", category=Category.CEX, analyst="alice@lab"),
                claim(
                    label="Mixer",
                    category=Category.MIXER,
                    source="internal list",
                    analyst="bob@lab",
                ),
            ]
        )
        store.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "Where sources disagree" in text
        assert "alice@lab" in text and "bob@lab" in text
        assert "Both claims are kept" in text


class TestProvenance:
    def test_absent_attestation_is_stated_not_omitted(self, tmp_path: Path) -> None:
        seed_notes(tmp_path).close()
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "No attestation" in args.out.read_text()

    def test_attestation_summary_is_carried_through(self, tmp_path: Path) -> None:
        seed_notes(tmp_path).close()
        (tmp_path / "attestation.json").write_text(
            '{"attested":"2026-08-01T00:00:00+00:00","queries_recorded":9,'
            '"responses":{"a":{"sha256":"x"},"b":{"sha256":"y"}},'
            '"queries_without_a_cached_response":["c"],"unreadable_audit_lines":2}'
        )
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "2 cached response(s) hashed" in text
        assert "2 unreadable audit line(s)" in text


class TestAttachments:
    def test_a_missing_companion_file_is_named(self, tmp_path: Path) -> None:
        # Silently dropping it would say nothing; naming it says something true.
        seed_notes(tmp_path).close()
        args = args_for(tmp_path, attach=[tmp_path / "gone.html"])
        report.run(args, TerminalRenderer())
        assert "not found when this was written" in args.out.read_text()

    def test_a_present_file_is_hashed(self, tmp_path: Path) -> None:
        seed_notes(tmp_path).close()
        graph = tmp_path / "flow.html"
        graph.write_text("<html></html>")
        args = args_for(tmp_path, attach=[graph])
        report.run(args, TerminalRenderer())
        assert "sha256" in args.out.read_text()


class TestHtml:
    def test_data_cannot_inject_markup(self, tmp_path: Path) -> None:
        log = CaseLog(tmp_path / "case.db")
        log.add(
            Note(
                at=NOW,
                analyst="<script>alert(1)</script>",
                identified_by="env",
                kind=NoteKind.OBSERVATION,
                body="<img src=x onerror=alert(1)>",
            )
        )
        log.close()

        args = args_for(tmp_path, out=tmp_path / "report.html")
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        # The angle brackets are what make markup; an `onerror=` that survives
        # inside escaped text is inert, so that is not what to assert on.
        assert "<script>" not in text
        assert "<img" not in text
        assert "&lt;script&gt;" in text
        assert "&lt;img src=x onerror=alert(1)&gt;" in text

    def test_carries_a_print_stylesheet(self, tmp_path: Path) -> None:
        # The whole PDF story rests on this, in place of a rendering dependency.
        seed_notes(tmp_path).close()
        args = args_for(tmp_path, out=tmp_path / "report.html")
        report.run(args, TerminalRenderer())
        assert "@media print" in args.out.read_text()

    def test_suffix_chooses_the_format(self, tmp_path: Path) -> None:
        seed_notes(tmp_path).close()
        report.run(args_for(tmp_path, out=tmp_path / "a.md"), TerminalRenderer())
        report.run(args_for(tmp_path, out=tmp_path / "b.html"), TerminalRenderer())
        assert (tmp_path / "a.md").read_text().startswith("# ")
        assert (tmp_path / "b.html").read_text().startswith("<!doctype html>")


class TestBoundary:
    def test_reports_the_frontier_as_a_boundary_not_an_ending(self, tmp_path: Path) -> None:
        from chainscope.core.chainid import ETHEREUM
        from chainscope.core.models import Address, Amount, Transfer, TransferKind, TxRef

        store = SqliteStore(tmp_path / "store.db")
        store.put_transfers(
            [
                Transfer(
                    chain=ETHEREUM,
                    tx=TxRef(ETHEREUM, "0x1"),
                    sender=Address(ETHEREUM, "0xaaa", "0xaaa"),
                    recipient=Address(ETHEREUM, "0xbbb", "0xbbb"),
                    amount=Amount(10**18, 18, "ETH"),
                    kind=TransferKind.NATIVE,
                )
            ]
        )
        store.put_attributions([claim()])
        store.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "seen and never followed" in text
        assert "not the end of the money" in text


class TestCorrespondenceInReport:
    def _sent(self, tmp_path: Path, **kw: object) -> int:
        from chainscope.case.correspondence import Ledger, Request, RequestKind

        fields: dict[str, object] = {
            "counterparty": "Binance",
            "kind": RequestKind.FREEZE,
            "sent_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "analyst": "alice@lab",
            "identified_by": "env",
        }
        fields.update(kw)
        ledger = Ledger(tmp_path / "case.db")
        try:
            return ledger.send(Request(**fields))  # type: ignore[arg-type]
        finally:
            ledger.close()

    def test_an_outstanding_request_is_something_not_yet_known(self, tmp_path: Path) -> None:
        # It belongs beside the open questions, not in an appendix: waiting on
        # an exchange is the commonest reason a case is unfinished.
        self._sent(tmp_path)
        args = args_for(tmp_path)
        assert report.run(args, TerminalRenderer()) == 0
        text = args.out.read_text()
        assert text.index("Waiting on somebody else") < text.index("## Narrative")

    def test_correspondence_alone_is_enough_to_report(self, tmp_path: Path) -> None:
        # No notes and no claims, but a freeze request was sent. That is a case.
        self._sent(tmp_path)
        args = args_for(tmp_path)
        assert report.run(args, TerminalRenderer()) == 0
        assert "Correspondence" in args.out.read_text()

    def test_an_overdue_request_is_marked(self, tmp_path: Path) -> None:
        self._sent(tmp_path, due_at=datetime(2026, 7, 8, tzinfo=timezone.utc))
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "past its deadline" in args.out.read_text()

    def test_an_answered_request_is_kept_but_not_listed_as_unknown(
        self, tmp_path: Path
    ) -> None:
        from chainscope.case.correspondence import Ledger, RequestEvent, Status

        rid = self._sent(tmp_path)
        ledger = Ledger(tmp_path / "case.db")
        ledger.record(
            rid,
            RequestEvent(
                at=datetime(2026, 7, 4, tzinfo=timezone.utc),
                status=Status.ANSWERED,
                analyst="alice@lab",
                identified_by="env",
                body="12.4 ETH held",
            ),
        )
        ledger.close()

        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "Not yet known" not in text
        assert "answered" in text


class TestHedgedClaims:
    def _tagged(self, tmp_path: Path, confidence: Confidence, rationale: str = "") -> None:
        store = SqliteStore(tmp_path / "store.db")
        store.put_attributions(
            [
                claim(
                    label="Probably Tornado",
                    category=Category.MIXER,
                    confidence=confidence,
                    rationale=rationale,
                )
            ]
        )
        store.close()

    def test_a_claim_below_high_carries_its_reasoning(self, tmp_path: Path) -> None:
        # The flow view hedges these on the face of the node. The report is the
        # artefact that leaves the building, and a MEDIUM label printed without
        # its reasoning is how a guess becomes a sentence somebody quotes.
        self._tagged(tmp_path, Confidence.MEDIUM, "three deposits in one block")
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "Claims below HIGH" in text
        assert "three deposits in one block" in text

    def test_a_high_claim_is_not_hedged(self, tmp_path: Path) -> None:
        self._tagged(tmp_path, Confidence.HIGH)
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "Claims below HIGH" not in args.out.read_text()

    def test_a_missing_rationale_is_named_not_omitted(self, tmp_path: Path) -> None:
        # MEDIUM does not require one, so this is reachable --- and a hedged
        # claim with nothing behind it is worth showing as exactly that.
        self._tagged(tmp_path, Confidence.MEDIUM)
        args = args_for(tmp_path)
        report.run(args, TerminalRenderer())
        assert "no rationale recorded" in args.out.read_text()

    def test_html_carries_it_too(self, tmp_path: Path) -> None:
        self._tagged(tmp_path, Confidence.LOW, "one shared funder")
        args = args_for(tmp_path, out=tmp_path / "r.html")
        report.run(args, TerminalRenderer())
        text = args.out.read_text()
        assert "Claims below HIGH" in text
        assert "one shared funder" in text
