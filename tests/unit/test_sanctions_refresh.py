"""Refreshing the screening snapshot, and saying what moved.

OFAC publishes SDN as XML on a schedule and this imported it by hand, so a
deployment's idea of who is sanctioned drifted by however long since somebody
remembered.

The snapshot stays what screening reads --- a pinned file is auditable and a
live fetch fails in the direction of "no match". These tests are mostly about
the refusals, because a screening list that quietly empties is the worst
failure this file can have.
"""

from __future__ import annotations

import json

from chainscope.cli.commands.sanctions import extract_addresses
from chainscope.cli.main import main

ETH_ADDR = "0x098b716b8aaf21512996dc57eb0615e2383e2f96"
BTC_ADDR = "bc1qa5wkgaew2dkv56kfvj49j0av5nml45x9ek9hz6"


def sdn(*ids: tuple[str, str], name: str = "LAZARUS GROUP") -> str:
    entries = "".join(
        f"<id><idType>Digital Currency Address - {t}</idType><idNumber>{a}</idNumber></id>"
        for t, a in ids
    )
    return (
        '<sdnList xmlns="http://tempuri.org/sdnList.xsd">'
        f"<sdnEntry><uid>1</uid><lastName>{name}</lastName>"
        f"<idList>{entries}</idList></sdnEntry></sdnList>"
    )


class TestExtraction:
    def test_an_ethereum_address_gets_its_chain(self):
        found = extract_addresses(sdn(("ETH", ETH_ADDR)))
        assert found[ETH_ADDR]["chain"] == "eip155:1"

    def test_bitcoin_too(self):
        found = extract_addresses(sdn(("XBT", BTC_ADDR)))
        assert found[BTC_ADDR]["chain"].startswith("bip122:")

    def test_an_unmapped_ticker_gets_no_chain(self):
        """A sanctions claim filed against the wrong chain looks answered, and
        the graph layer trusts a chain-scoped claim."""
        found = extract_addresses(sdn(("ZEC", "t1abc")))
        assert "chain" not in found["t1abc"]

    def test_but_the_ticker_is_recorded(self):
        """So a reader sees it was unmapped rather than assuming the address was
        meant to apply everywhere."""
        found = extract_addresses(sdn(("ZEC", "t1abc")))
        assert found["t1abc"]["currency_hint"] == "ZEC"

    def test_the_designated_party_is_in_the_label(self):
        found = extract_addresses(sdn(("ETH", ETH_ADDR)))
        assert "LAZARUS GROUP" in found[ETH_ADDR]["label"]

    def test_non_currency_identifiers_are_ignored(self):
        xml = (
            "<sdnList><sdnEntry><lastName>X</lastName><idList>"
            "<id><idType>Passport</idType><idNumber>ABC123</idNumber></id>"
            "</idList></sdnEntry></sdnList>"
        )
        assert extract_addresses(xml) == {}


class TestTheDiffIsTheOutput:
    def test_a_first_run_lists_everything_as_added(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        assert main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"]) == 0
        assert f"+ {ETH_ADDR}" in capsys.readouterr().out

    def test_an_unchanged_list_says_so(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        capsys.readouterr()
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        assert "no change" in capsys.readouterr().out

    def test_a_removal_is_called_a_delisting(self, tmp_path, monkeypatch, capsys):
        """Delisted and deleted mean opposite things about an address sitting in
        somebody's open case."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR), ("XBT", BTC_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        capsys.readouterr()
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        assert "delisted" in capsys.readouterr().out

    def test_check_writes_nothing_and_flags_a_change(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        assert (
            main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json", "--check"]) == 1
        )
        assert not (tmp_path / "snap.json").exists()


class TestItRefusesToEmptyTheList:
    def test_a_document_with_no_addresses_does_not_overwrite(
        self, tmp_path, monkeypatch, capsys
    ):
        """A format change means the parser broke, not that sanctions were
        lifted. Overwriting here would empty the screening list."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        before = (tmp_path / "snap.json").read_text()
        capsys.readouterr()

        (tmp_path / "sdn.xml").write_text("<sdnList></sdnList>")
        assert main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"]) == 1
        assert (tmp_path / "snap.json").read_text() == before
        assert "format has" in capsys.readouterr().err

    def test_an_unreadable_document_is_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text("not xml at all <<<")
        assert main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"]) == 1

    def test_a_missing_file_is_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["sanctions", "--from-file", "absent.xml"]) == 1


class TestTheSnapshotIsAuditable:
    def test_it_records_when_and_from_where(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        data = json.loads((tmp_path / "snap.json").read_text())
        assert data["source"] == "sdn.xml"
        assert data["fetched"].endswith("+00:00")

    def test_the_shape_is_what_ofacsource_reads(self, tmp_path, monkeypatch):
        """Written to be loaded by the existing offline source, not by a new
        one --- screening does not become live."""
        from chainscope.attribution.sources.ofac import OfacSource

        monkeypatch.chdir(tmp_path)
        (tmp_path / "sdn.xml").write_text(sdn(("ETH", ETH_ADDR)))
        main(["sanctions", "--from-file", "sdn.xml", "--out", "snap.json"])
        claims = OfacSource(tmp_path / "snap.json").lookup(ETH_ADDR)
        assert claims
