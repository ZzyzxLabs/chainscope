"""Leads, from the moment somebody finds one to the moment it is settled.

`chainscope.osint.leads` has defined what a lead *is* since early on --- never
an attribution, always carrying the step that would settle it --- and had **no
callers**. Nothing produced one, nothing stored one, no command showed one. §2
of `docs/needs.md` names that failure: a technique nobody can reach does not
exist.

The design questions here are all about what to keep, and the answers all go
the same way:

* A refuted lead is kept. It is the record that somebody already looked, which
  is what stops the next analyst repeating the search --- and in a shared case,
  stops two people doing it simultaneously.
* A verdict cannot be recorded without a reason. "Confirmed" with no basis
  reads, once its author has moved on, exactly like a guess.
* Filing the same lead twice returns the existing one *and says so*. The id
  looks identical either way, and "already known, already refuted" is the most
  useful thing the tool can say to somebody about to spend an hour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chainscope.case.leads import LeadStore, Verdict
from chainscope.osint.leads import Lead

ADDRESS = "0x28c6c06298d514db089934071355e5743bf21d60"


def _lead(value: str = "alice", kind: str = "twitter") -> Lead:
    return Lead(
        address=ADDRESS,
        kind=kind,
        value=value,
        source="ENS text record com.twitter on foo.eth",
        asserted_by="the owner of foo.eth",
        verify_by="check whether that account has published this address itself",
    )


@pytest.fixture
def store(tmp_path: Path) -> LeadStore:
    s = LeadStore(tmp_path / "case.db")
    yield s
    s.close()


class TestFiling:
    def test_a_lead_survives_a_round_trip(self, store: LeadStore) -> None:
        lead_id, was_new = store.add(_lead(), "alice@lab")
        assert was_new
        assert store.get(lead_id).value == "alice"

    def test_the_verification_step_is_carried_not_regenerated(self, store: LeadStore) -> None:
        # Written when the lead was found, by whoever understood why it
        # mattered. Recomputing it later loses that.
        lead_id, _ = store.add(_lead(), "alice@lab")
        assert "published this address itself" in store.get(lead_id).verify_by

    def test_a_lead_starts_open(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        assert store.get(lead_id).is_open

    def test_filing_twice_gives_one_lead(self, store: LeadStore) -> None:
        first, _ = store.add(_lead(), "alice@lab")
        second, was_new = store.add(_lead(), "bob@lab")
        assert first == second
        assert not was_new
        assert len(store.leads()) == 1

    def test_the_caller_is_told_it_was_not_new(self, store: LeadStore) -> None:
        # It cannot infer this: the id is identical either way.
        store.add(_lead(), "alice@lab")
        assert store.add(_lead(), "bob@lab")[1] is False

    def test_a_different_value_is_a_different_lead(self, store: LeadStore) -> None:
        store.add(_lead("alice"), "alice@lab")
        store.add(_lead("bob"), "alice@lab")
        assert len(store.leads()) == 2

    def test_the_address_is_folded_for_lookup(self, store: LeadStore) -> None:
        store.add(_lead(), "alice@lab")
        assert len(store.leads(ADDRESS.upper())) == 1


class TestSettling:
    def test_a_verdict_needs_a_reason(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        with pytest.raises(ValueError, match="needs a reason"):
            store.settle(lead_id, Verdict.CONFIRMED, "   ", "alice@lab")

    def test_the_refusal_says_why_it_matters(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        with pytest.raises(ValueError, match="indistinguishable from a guess"):
            store.settle(lead_id, Verdict.CONFIRMED, "", "alice@lab")

    def test_a_settled_lead_keeps_its_reason_and_author(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        record = store.settle(lead_id, Verdict.REFUTED, "profile predates it", "bob@lab")
        assert record.verdict == Verdict.REFUTED
        assert record.reason == "profile predates it"
        assert record.settled_by == "bob@lab"
        assert record.settled_at is not None

    def test_a_refuted_lead_is_not_deleted(self, store: LeadStore) -> None:
        # The whole design. Without this the next analyst repeats the search.
        lead_id, _ = store.add(_lead(), "alice@lab")
        store.settle(lead_id, Verdict.REFUTED, "checked, nothing there", "alice@lab")
        assert len(store.leads()) == 1

    def test_refiling_a_settled_lead_does_not_reopen_it(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        store.settle(lead_id, Verdict.REFUTED, "checked", "alice@lab")
        again, was_new = store.add(_lead(), "bob@lab")
        assert again == lead_id and not was_new
        assert store.get(lead_id).verdict == Verdict.REFUTED
        assert store.get(lead_id).reason == "checked"

    def test_it_cannot_be_settled_as_open(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "alice@lab")
        with pytest.raises(ValueError, match="cannot be settled as 'open'"):
            store.settle(lead_id, Verdict.OPEN, "changed my mind", "alice@lab")

    def test_unreachable_is_not_refuted(self, store: LeadStore) -> None:
        # "The account was deleted" and "the claim is false" are different
        # findings, and collapsing them loses that somebody spent the time.
        lead_id, _ = store.add(_lead(), "alice@lab")
        record = store.settle(lead_id, Verdict.UNREACHABLE, "account deleted", "alice@lab")
        assert record.verdict != Verdict.REFUTED
        assert not record.is_open

    def test_settling_something_that_does_not_exist_says_so(self, store: LeadStore) -> None:
        with pytest.raises(ValueError, match="no lead 99"):
            store.settle(99, Verdict.CONFIRMED, "because", "alice@lab")


class TestReading:
    def test_open_leads_exclude_settled_ones(self, store: LeadStore) -> None:
        first, _ = store.add(_lead("alice"), "a@lab")
        store.add(_lead("bob"), "a@lab")
        store.settle(first, Verdict.REFUTED, "checked", "a@lab")
        assert [lead.value for lead in store.open_leads()] == ["bob"]

    def test_listing_shows_settled_ones_by_default(self, store: LeadStore) -> None:
        lead_id, _ = store.add(_lead(), "a@lab")
        store.settle(lead_id, Verdict.REFUTED, "checked", "a@lab")
        assert len(store.leads()) == 1

    def test_the_summary_counts_every_verdict_including_zero(self, store: LeadStore) -> None:
        # So a reader can tell "nothing was refuted" from "there is no such
        # category here".
        store.add(_lead(), "a@lab")
        counts = store.summary()
        assert counts["open"] == 1
        assert counts["refuted"] == 0
        assert set(counts) == {v.value for v in Verdict}

    def test_it_survives_reopening_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "case.db"
        first = LeadStore(path)
        lead_id, _ = first.add(_lead(), "a@lab")
        first.settle(lead_id, Verdict.CONFIRMED, "they published it", "a@lab")
        first.close()

        again = LeadStore(path)
        try:
            assert again.get(lead_id).reason == "they published it"
        finally:
            again.close()


class TestItIsReachable:
    """The actual defect: the module existed and nothing could get to it."""

    def test_the_cli_registers_it(self) -> None:
        from chainscope.cli.main import _COMMANDS

        assert "lead" in _COMMANDS

    def test_a_lead_still_refuses_to_exist_without_a_verification_step(self) -> None:
        # The property `osint.leads` was built around, checked through the path
        # that now actually reaches it.
        with pytest.raises(ValueError, match="verification step"):
            Lead(
                address=ADDRESS,
                kind="twitter",
                value="alice",
                source="somewhere",
                asserted_by="somebody",
                verify_by="  ",
            )
