"""Local label files: what they claim, and where that claim applies."""

from __future__ import annotations

from chainscope.attribution.sources.local import LocalSource
from chainscope.core.chainid import ETHEREUM


class TestARecordDoesNotLeakAcrossChains:
    """The same twenty bytes exist on every EVM chain.

    A label somebody scoped to BSC is not evidence about the Ethereum address
    sharing its hex, and returning it contaminates an unrelated address with a
    claim its owner never made. `tag`'s own docs name this risk; the local
    source did it anyway.
    """

    def _source(self, tmp_path):
        import json

        path = tmp_path / "labels.json"
        path.write_text(
            json.dumps(
                {
                    "0x" + "a" * 40: {
                        "label": "BSC hot wallet",
                        "category": "cex",
                        "confidence": "high",
                        "source": "team",
                        "chain": "eip155:56",
                    },
                    "0x" + "b" * 40: {
                        "label": "OFAC listing",
                        "category": "sanctioned",
                        "confidence": "high",
                        "source": "ofac",
                    },
                    "0x" + "c" * 40: {
                        "label": "broken scope",
                        "category": "cex",
                        "confidence": "high",
                        "source": "team",
                        "chain": "56",
                    },
                }
            )
        )
        return LocalSource(path)

    def test_a_bsc_label_is_not_returned_for_ethereum(self, tmp_path):
        assert self._source(tmp_path).lookup("0x" + "a" * 40, ETHEREUM) == []

    def test_it_is_returned_for_its_own_chain(self, tmp_path):
        from chainscope.core.chainid import ChainId

        got = self._source(tmp_path).lookup("0x" + "a" * 40, ChainId.parse("eip155:56"))
        assert [c.label for c in got] == ["BSC hot wallet"]

    def test_a_chain_agnostic_record_still_applies_everywhere(self, tmp_path):
        # How sanctions lists are published. Narrowing them to one chain would
        # be the opposite error.
        from chainscope.core.chainid import ChainId

        source = self._source(tmp_path)
        for chain in (ETHEREUM, ChainId.parse("eip155:56"), None):
            got = source.lookup("0x" + "b" * 40, chain)
            assert [c.label for c in got] == ["OFAC listing"]

    def test_an_unreadable_scope_applies_nowhere(self, tmp_path):
        """It says it is scoped and nobody can tell to what.

        Guessing invents a claim in either direction --- about a chain the file
        never named, or about all of them.
        """
        from chainscope.core.chainid import ChainId

        source = self._source(tmp_path)
        for chain in (ETHEREUM, ChainId.parse("eip155:56"), None):
            assert source.lookup("0x" + "c" * 40, chain) == []

    def test_an_unscoped_query_keeps_the_records_own_chain(self, tmp_path):
        # And does not stamp it with the caller's, which is what the fallback
        # used to do: a BSC label emitted as an Ethereum claim.
        got = self._source(tmp_path).lookup("0x" + "a" * 40, None)
        assert str(got[0].chain) == "eip155:56"

    def test_lookup_many_scopes_the_same_way(self, tmp_path):
        found = self._source(tmp_path).lookup_many(["0x" + "a" * 40, "0x" + "b" * 40], ETHEREUM)
        assert found["0x" + "a" * 40] == []
        assert [c.label for c in found["0x" + "b" * 40]] == ["OFAC listing"]
