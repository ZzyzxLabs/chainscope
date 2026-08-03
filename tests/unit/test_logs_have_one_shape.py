"""Every provider's logs must arrive in the same shape.

Two providers exist here so one can catch the other being wrong. That only
works if a caller written against one does not silently return a different
answer from the other — and Blockscout's Etherscan-compatible endpoint differs
from JSON-RPC in two ways that are silent rather than loud:

  * topics are padded to four with nulls, so the ERC-20 rule "exactly three
    topics" rejects every record;
  * there is no `blockHash`, so deduplicating by the standard log identity
    keys on None and folds unrelated records together.

Measured on a real case: fifteen valid transfers became zero. An empty set does
not look like a mistake, which is exactly why this is normalised at the
provider boundary rather than in each caller.
"""

from __future__ import annotations

from chainscope.providers.blockscout import _normalise_log

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FROM = "0x0000000000000000000000001c6e28d3f5175e9093de62a188d87c5ba8148b4d"
TO = "0x000000000000000000000000f600c14e09c8997851b732d079d3b8e7b357980b"


def test_trailing_null_topics_are_dropped() -> None:
    """`len(topics) == 3` is the ERC-20 test; four with a null fails it."""
    row = _normalise_log({"topics": [TRANSFER, FROM, TO, None], "data": "0x" + "0" * 64})
    assert row["topics"] == [TRANSFER, FROM, TO]
    assert len(row["topics"]) == 3


def test_a_genuine_fourth_topic_survives() -> None:
    """ERC-721 has four real topics and must stay distinguishable from ERC-20."""
    row = _normalise_log({"topics": [TRANSFER, FROM, TO, "0x" + "1" * 64]})
    assert len(row["topics"]) == 4


def test_block_hash_is_always_present_as_a_key() -> None:
    """So a dedup key cannot silently read a missing field on one provider."""
    assert "blockHash" in _normalise_log({"topics": []})


def test_a_supplied_block_hash_is_kept() -> None:
    row = _normalise_log({"topics": [], "blockHash": "0xabc"})
    assert row["blockHash"] == "0xabc"


def test_nothing_else_is_touched() -> None:
    """Normalising must not become editing."""
    original = {
        "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "data": "0x" + "0" * 63 + "1",
        "logIndex": "0xde",
        "transactionHash": "0x6e98",
        "topics": [TRANSFER],
    }
    row = _normalise_log(original)
    for field in ("address", "data", "logIndex", "transactionHash"):
        assert row[field] == original[field]
    # And the input is not mutated: callers may still hold it.
    assert original["topics"] == [TRANSFER]
