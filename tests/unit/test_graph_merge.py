"""Merging two sightings of one address without inventing confidence."""

from __future__ import annotations

from chainscope.core.attribution import Confidence
from chainscope.render.graph import Graph, Node


class TestMergedConfidenceCannotExceedItsContributors:
    """A node built from two claims must not be reported above the weaker one.

    Confidence used to be the winner's whenever the winner supplied *any*
    field. So a HIGH sighting carrying only a category, merged with a
    SPECULATIVE claim carrying a label, produced "Probably Lazarus" at HIGH ---
    and the flow view drops its `?` hedge at HIGH, so a hunch was drawn as an
    identification. The merge invented the confidence; no source claimed it.
    """

    def _merge(self, first, second):
        graph = Graph(seeds=["eip155:1:0xaaa"])
        graph.add_node(first)
        graph.add_node(second)
        return graph.nodes["eip155:1:0xaaa"]

    def _node(self, label, category, confidence, source="s"):
        return Node(
            address="0xaaa",
            chain="eip155:1",
            label=label,
            category=category,
            confidence=int(confidence),
            source=source,
        )

    def test_a_label_from_the_weaker_claim_keeps_its_own_confidence(self):
        merged = self._merge(
            self._node("", "cex", Confidence.HIGH),
            self._node("Probably Lazarus", "", Confidence.SPECULATIVE),
        )
        assert merged.label == "Probably Lazarus"
        assert merged.confidence == int(Confidence.SPECULATIVE)

    def test_the_stronger_claim_keeps_its_confidence_when_it_supplied_the_fields(self):
        merged = self._merge(
            self._node("Binance 14", "cex", Confidence.HIGH),
            self._node("", "", Confidence.SPECULATIVE),
        )
        assert merged.label == "Binance 14"
        assert merged.confidence == int(Confidence.HIGH)

    def test_two_equal_claims_are_unchanged(self):
        merged = self._merge(
            self._node("Binance 14", "cex", Confidence.HIGH, "explorer"),
            self._node("Binance 14", "cex", Confidence.HIGH, "other"),
        )
        assert merged.confidence == int(Confidence.HIGH)

    def test_when_neither_claims_anything_the_winner_stands(self):
        # Nothing is being asserted, so there is no field whose strength could
        # be overstated.
        merged = self._merge(
            self._node("", "", Confidence.HIGH),
            self._node("", "", Confidence.LOW),
        )
        assert merged.confidence == int(Confidence.HIGH)
