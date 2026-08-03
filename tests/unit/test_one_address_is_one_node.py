"""An address must be one node, whatever case the person typed.

Seen on screen: the sidebar listed `0x098B71…3E2f96` and `0x098b71…3e2f96`
as separate addresses, and the graph drew the seed twice --- two boxes at the
same place, its flows split between them. The seed enters as somebody typed it,
checksummed; every counterparty comes back lowercase from the store; `Graph`
keyed nodes by the raw string.

The visible symptom was overlapping labels. The real one was worse: a graph
that splits an address in two understates how much passed through it, and no
part of the page says anything is wrong.
"""

from __future__ import annotations

from chainscope.render.graph import Edge, Graph, Node

CHAIN = "eip155:1"
CHECKSUM = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
LOWER = CHECKSUM.lower()
OTHER = "0x8125e2b1f8bd2a0e4c0d8bd8bd7c2c1d9c24fb40"


def test_the_same_evm_address_in_two_cases_is_one_node() -> None:
    graph = Graph()
    graph.add_node(Node(address=CHECKSUM, chain=CHAIN, is_seed=True))
    graph.add_node(Node(address=LOWER, chain=CHAIN))
    assert len(graph.nodes) == 1, (
        "the seed was drawn twice --- once as typed, once as the store spells it"
    )


def test_edges_between_the_same_pair_fold_across_case() -> None:
    """Otherwise one flow is counted as two, each with half the money."""
    graph = Graph()
    graph.add_edge(Edge(source=CHECKSUM, target=OTHER, chain=CHAIN, symbol="ETH", total_raw=10))
    graph.add_edge(Edge(source=LOWER, target=OTHER, chain=CHAIN, symbol="ETH", total_raw=5))
    assert len(graph.edges) == 1
    assert next(iter(graph.edges.values())).total_raw == 15


def test_emitted_edge_endpoints_name_emitted_node_ids() -> None:
    """The page matches these strings; one rule has to produce both."""
    graph = Graph()
    graph.add_node(Node(address=CHECKSUM, chain=CHAIN, is_seed=True))
    graph.add_node(Node(address=OTHER, chain=CHAIN))
    graph.add_edge(Edge(source=LOWER, target=OTHER, chain=CHAIN, symbol="ETH", total_raw=1))
    ids = {n.to_dict()["id"] for n in graph.nodes.values()}
    for edge in graph.edges.values():
        payload = edge.to_dict()
        assert payload["source"] in ids, "an edge that attaches to no node is not drawn"
        assert payload["target"] in ids


def test_a_case_sensitive_chain_is_left_alone() -> None:
    """Folding Solana would merge two people. Never do it to be tidy."""
    sol = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    upper = "So11111111111111111111111111111111111111112"
    mixed = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    graph = Graph()
    graph.add_node(Node(address=upper, chain=sol))
    graph.add_node(Node(address=mixed, chain=sol))
    assert len(graph.nodes) == 2


def test_the_server_hands_the_page_the_identity_it_uses() -> None:
    """So the rule lives in one language, not two."""
    from pathlib import Path

    handler = Path("src/chainscope/server/local.py").read_text()
    assert '"key": address_key(chain, address) if chain else address' in handler
    page = Path("src/chainscope/server/webapp.py").read_text()
    assert "found.key" in page, "the page still matched on the address as typed"
