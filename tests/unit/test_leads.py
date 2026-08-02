"""A lead is somewhere to look. It must never read as something concluded."""

from __future__ import annotations

import pytest

from chainscope.attribution.ens import EnsRecord
from chainscope.osint.leads import TEXT_KEYS, Lead, leads_from_text_records

ADDR = "0x" + "a" * 40
OTHER = "0x" + "b" * 40


def confirmed(name: str = "alice.eth") -> EnsRecord:
    return EnsRecord(address=ADDR, name=name, forward_address=ADDR)


def unconfirmed(name: str = "binance-hot.eth") -> EnsRecord:
    # Somebody else pointed a name at this address. Classic impersonation shape.
    return EnsRecord(address=ADDR, name=name, forward_address=OTHER)


class TestALeadCarriesItsVerification:
    def test_one_is_required(self) -> None:
        with pytest.raises(ValueError, match="rumour"):
            Lead(
                address=ADDR,
                kind="twitter",
                value="alice",
                source="somewhere",
                asserted_by="someone",
                verify_by="  ",
            )

    def test_a_value_is_required(self) -> None:
        with pytest.raises(ValueError, match="needs a value"):
            Lead(
                address=ADDR,
                kind="twitter",
                value="",
                source="s",
                asserted_by="a",
                verify_by="v",
            )

    def test_every_generated_lead_has_one(self) -> None:
        leads = leads_from_text_records(confirmed(), {key: "value" for key in TEXT_KEYS})
        assert leads
        assert all(lead.verify_by.strip() for lead in leads)

    def test_who_asserted_it_is_on_the_face_of_it(self) -> None:
        # Not in a footnote. The risk in a lead is reading it as a fact about
        # the address rather than a claim by whoever set the record.
        lead = leads_from_text_records(confirmed(), {"com.twitter": "alice"})[0]
        assert "owner of alice.eth" in lead.asserted_by
        assert "asserted by" in str(lead)


class TestItRefusesAnUnconfirmedName:
    def test_no_leads_at_all(self) -> None:
        """An unconfirmed name is a stranger's claim about this address.

        Its text records are then that stranger's handles, and attaching them
        here would put another person's identity on this address --- worse than
        finding nothing.
        """
        assert leads_from_text_records(unconfirmed(), {"com.twitter": "binance"}) == []

    def test_nor_from_an_unchecked_one(self) -> None:
        record = EnsRecord(address=ADDR, name="alice.eth", forward_address=None)
        assert leads_from_text_records(record, {"com.twitter": "alice"}) == []

    def test_nor_when_there_is_no_name(self) -> None:
        assert leads_from_text_records(EnsRecord(address=ADDR), {"url": "x"}) == []


class TestWhatItReads:
    def test_known_keys_become_leads(self) -> None:
        leads = leads_from_text_records(
            confirmed(), {"com.twitter": "alice", "url": "https://example.com"}
        )
        assert {lead.kind for lead in leads} == {"twitter", "url"}

    def test_an_unknown_key_is_skipped(self) -> None:
        # A resolver holds anything. A lead named after a key nobody recognises
        # reads as a finding about a field the reader assumes was understood.
        leads = leads_from_text_records(
            confirmed(), {"com.example.private": "secret", "com.twitter": "alice"}
        )
        assert [lead.kind for lead in leads] == ["twitter"]

    def test_a_blank_value_is_not_a_lead(self) -> None:
        assert leads_from_text_records(confirmed(), {"com.twitter": "   "}) == []

    def test_no_records_means_no_leads(self) -> None:
        assert leads_from_text_records(confirmed(), None) == []

    def test_the_source_names_where_to_look_again(self) -> None:
        lead = leads_from_text_records(confirmed(), {"com.github": "alice"})[0]
        assert "com.github" in lead.source and "alice.eth" in lead.source


class TestItIsNotAnAttribution:
    def test_leads_do_not_expose_a_confidence(self) -> None:
        """The type must not be mistakable for a claim about the address.

        No confidence, no category, no `label` --- an `Attribution` says what an
        address *is*, and nothing here supports that.
        """
        lead = leads_from_text_records(confirmed(), {"com.twitter": "alice"})[0]
        for attribute in ("confidence", "category", "label", "method"):
            assert not hasattr(lead, attribute)
