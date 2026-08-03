"""Edges in the drawing must attach to the nodes that were stored.

`_node_payload` keys nodes by `address_key`. `_edge_payload` and the BFS that
assigns hop depth both used `.lower()`. On Ethereum those agree and nothing
looks wrong; on Solana, Sui or Bitcoin they do not, and the disagreement is
silent --- edges reference endpoints that match no node, and every node lands in
column zero because the depth lookup misses.

The same defect as the `_fold`/`_key` mismatch in the resolver: a normalisation
rule applied to one half of a pair. It stays invisible on the chain everybody
tests with, which is exactly why it needs a test on a chain that is not.
"""

from __future__ import annotations

from chainscope.render.flow import _edge_payload, _node_payload, layer_nodes
from chainscope.render.graph import Edge, Graph, Node

# Real base58: distinct addresses that differ only in case. `.lower()` folds
# these into one; they belong to two different people.
UPPER = "So11111111111111111111111111111111111111112"
MIXED = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SINK = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
SOL = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def _graph() -> Graph:
    graph = Graph(seeds=[f"{SOL}:{UPPER}"])
    for address in (UPPER, MIXED, SINK):
        graph.add_node(Node(address=address, chain=SOL))
    graph.add_edge(Edge(source=UPPER, target=SINK, chain=SOL, symbol="SOL", total_raw=1))
    graph.add_edge(Edge(source=MIXED, target=SINK, chain=SOL, symbol="SOL", total_raw=2))
    return graph


def test_every_edge_endpoint_names_a_node_that_exists() -> None:
    """A dangling endpoint is an edge drawn to nowhere."""
    graph = _graph()
    depth = layer_nodes(graph)
    ids = {_node_payload(n, depth.get(n.address, 0), 99)["id"] for n in graph.nodes.values()}
    for edge in [_edge_payload(e) for e in graph.edges.values()]:
        assert edge["source"] in ids, f"edge from {edge['source']} attaches to no node"
        assert edge["target"] in ids, f"edge to {edge['target']} attaches to no node"


def test_case_distinct_addresses_stay_distinct() -> None:
    """Two people must not be merged into one because of a `.lower()`."""
    edges = [
        _edge_payload(Edge(source=a, target=SINK, chain=SOL, symbol="SOL", total_raw=1))
        for a in (UPPER, MIXED)
    ]
    assert edges[0]["source"] != edges[1]["source"]
    # And the stored form survives: base58 is case-sensitive, so the payload
    # must carry what was written, not a folded copy of it.
    assert edges[0]["source"] == UPPER
    assert edges[1]["source"] == MIXED


def test_hop_depth_survives_a_case_sensitive_chain() -> None:
    """The seed is hop 0 and its counterparty is hop 1, not all-zero."""
    depths = layer_nodes(_graph())
    assert depths[UPPER] == 0, "the seed should be at hop zero"
    assert depths[SINK] == 1, (
        "the seed's counterparty should be one hop out; if this is 0 the depth "
        "map was keyed differently from the nodes and every lookup missed"
    )
