"""MCP server: the store, the graph, and the label path, reachable by an agent.

Natural language is a genuinely good fit for this work --- "what did this
address send to exchanges in August" is easier to say than to write as SQL, and
an investigator's questions arrive in that shape. The awkward part is that an
agent is also a confident narrator, and a forensics tool whose output can be
paraphrased into certainty is worse than no tool.

So three things are built in rather than left to prompting.

**Every claim carries its provenance in the response.** Not a footnote: the
label, the source, the confidence, and the rationale are fields on the same
object. An agent that wants to state "this is Binance" has the string
"confidence: MEDIUM, source: nametag dump" in front of it, and omitting that is
then a visible choice rather than an accident of formatting.

**Writing is opt-in and self-identifying.** ``--writable`` is off by default,
and when a label does get written the source records which agent wrote it. An
investigator reviewing the store six months later has to be able to tell a
human's judgement from a model's suggestion, and there is no way to recover
that after the fact.

**Amounts are strings.** Every number that crosses this boundary is wei-scale,
JSON numbers are IEEE 754 doubles, and 10 ETH already exceeds what one holds
exactly. A silently rounded balance is the kind of error that survives all the
way into a report.

Uncertainty is reported as uncertainty. A query that could not complete says so
rather than returning fewer rows, because an agent cannot distinguish a short
list from a truncated one and will summarise either as "I found three".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..store.base import Query
from ..store.sqlite import SqliteStore

if TYPE_CHECKING:  # pragma: no cover
    # Imported for types only. The runtime import is deferred so that
    # `import chainscope` does not require the optional MCP SDK.
    from mcp.server.mcpserver import MCPServer

__all__ = ["AgentError", "ServerConfig", "build_server", "main"]

#: What an agent-written label is sourced as. Deliberately not configurable to
#: something anonymous: the point is that a human reading the store later can
#: tell which claims came from a model.
AGENT_SOURCE_PREFIX = "agent:"


class AgentError(RuntimeError):
    """The request could not be served."""


def _mcp() -> type[MCPServer]:
    try:
        from mcp.server.mcpserver import MCPServer as _MCPServer
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AgentError(
            "the agent server needs the MCP SDK: pip install 'chainscope[agent]'"
        ) from exc
    return _MCPServer


@dataclass
class ServerConfig:
    store: Path = Path(".chainscope/store.db")
    view: Path | None = None
    """DuckDB analytical view. Built on demand from the store if absent."""

    writable: bool = False
    """Whether the agent may record labels. Off by default."""

    agent_name: str = "unknown-agent"
    max_rows: int = 500
    """Hard cap on any result. Exceeding it is reported, never silently applied
    --- an agent cannot tell a short list from a truncated one."""


def _chain(raw: str | None) -> ChainId | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.isdigit():
        return ChainId.evm(int(text))
    namespace, _, reference = text.partition(":")
    return ChainId(namespace, reference) if reference else None


def _cap(limit: int, ceiling: int) -> int:
    """Clamp a requested limit into something a slice can mean.

    An agent passing -1 would otherwise reach ``rows[:-1]``, silently dropping
    the last result and reporting the rest as complete. Zero would return
    nothing and look like an empty answer.
    """
    return max(1, min(limit, ceiling))


def _amount(raw: int, decimals: int, symbol: str) -> dict[str, Any]:
    """Amounts leave as strings. See the module docstring."""
    return {"raw": str(raw), "decimals": decimals, "symbol": symbol}


def _claim(a: Attribution) -> dict[str, Any]:
    """One attribution, with everything needed to judge it.

    ``confidence`` appears as both a name and a number so that neither a
    human-readable summary nor a comparison needs to re-derive the other.
    """
    return {
        "label": a.label,
        "category": a.category.value,
        "confidence": a.confidence.name,
        "confidence_value": int(a.confidence),
        "method": a.method.value,
        "source": a.source,
        "rationale": a.rationale,
        "chain": str(a.chain) if a.chain else None,
    }


def build_server(config: ServerConfig) -> MCPServer:
    """Construct the MCP server. Kept separate from :func:`main` so tests can
    call the tool functions directly without a transport."""
    server = _mcp()(
        name="chainscope",
        instructions=(
            "Blockchain forensics over a local store. Every attribution comes "
            "with a source and a confidence; report them alongside any claim "
            "you make, and do not upgrade a MEDIUM label into a statement of "
            "fact. Amounts are strings because they exceed what a JSON number "
            "holds exactly --- do not convert them to floats. If a result says "
            "it was truncated, say so rather than summarising it as complete."
        ),
    )

    def _store() -> SqliteStore:
        if not config.store.exists():
            raise AgentError(
                f"no store at {config.store}. Run an analysis first, or point "
                f"--store at an existing one."
            )
        return SqliteStore(config.store)

    # ------------------------------------------------------------------ reading

    @server.tool(
        description=(
            "Everything known about an address: who it is, who says so, and how "
            "confident that claim is. Returns all claims, including ones that "
            "disagree --- disagreement is often the finding."
        )
    )
    def resolve_address(address: str) -> dict[str, Any]:
        store = _store()
        try:
            claims = store.attributions(address)
        finally:
            store.close()
        return {
            "address": address,
            "claims": [_claim(a) for a in claims],
            "claim_count": len(claims),
            "note": (
                "No attribution found. That means nobody has labelled this "
                "address in this store --- not that it is unlabelled anywhere, "
                "and certainly not that it is benign."
                if not claims
                else ""
            ),
        }

    @server.tool(
        description=(
            "Aggregate value flow to or from an address, largest first. One "
            "counterparty is one row regardless of how many transfers it took."
        )
    )
    def flows(
        address: str,
        direction: str = "out",
        chain: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if direction not in ("out", "in"):
            raise AgentError("direction must be 'out' or 'in'")
        capped = _cap(limit, config.max_rows)
        store = _store()
        try:
            edges = store.edges(address, _chain(chain) or ChainId.evm(1), direction=direction)
        finally:
            store.close()

        edges.sort(key=lambda e: e.total_raw, reverse=True)
        shown = edges[:capped]
        return {
            "address": address,
            "direction": direction,
            "flows": [
                {
                    "counterparty": e.recipient if direction == "out" else e.sender,
                    "total": str(e.total_raw),
                    "asset": e.asset,
                    "transfers": e.transfer_count,
                    "first_seen": e.first_seen.isoformat() if e.first_seen else None,
                    "last_seen": e.last_seen.isoformat() if e.last_seen else None,
                }
                for e in shown
            ],
            "shown": len(shown),
            "total_available": len(edges),
            "truncated": len(edges) > len(shown),
        }

    @server.tool(
        description=(
            "Search stored transfers by any combination of sender, recipient, "
            "asset, amount range, and block range. Amounts are raw integer "
            "strings in the asset's smallest unit."
        )
    )
    def search_transfers(
        address: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        asset: str | None = None,
        min_amount: str | None = None,
        max_amount: str | None = None,
        chain: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        capped = _cap(limit, config.max_rows)
        try:
            floor = int(min_amount) if min_amount else None
            ceiling = int(max_amount) if max_amount else None
        except ValueError as exc:
            raise AgentError(
                f"min_amount and max_amount are raw integer strings in the "
                f"asset's smallest unit, not decimals: {exc}"
            ) from exc
        query = Query(
            chain=_chain(chain),
            address=address,
            sender=sender,
            recipient=recipient,
            asset=asset,
            min_amount=floor,
            max_amount=ceiling,
            # One more than asked for, so "there is more" is a fact rather than
            # an inference from a full page.
            limit=capped + 1,
        )
        store = _store()
        try:
            rows = list(store.transfers(query))
        finally:
            store.close()

        truncated = len(rows) > capped
        return {
            "query": query.describe(),
            "transfers": [
                {
                    "tx": t.tx.hash,
                    "chain": str(t.chain),
                    "from": t.sender.key if t.sender else None,
                    "to": t.recipient.key if t.recipient else None,
                    "amount": _amount(t.amount.raw, t.amount.decimals, t.amount.symbol),
                    "kind": t.kind.value,
                    "block": t.block,
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                }
                for t in rows[:capped]
            ],
            "shown": min(len(rows), capped),
            "truncated": truncated,
            "note": (
                "More results exist than were returned. Narrow the query rather "
                "than treating this as the complete set."
                if truncated
                else ""
            ),
        }

    @server.tool(
        description=(
            "Run a read-only SQL query against the analytical view. Tables: "
            "transfers(chain, tx_hash, sender, recipient, amount_raw, decimals, "
            "symbol, asset, kind, block, timestamp) and attributions(address, "
            "chain, label, category, confidence, method, source, rationale). "
            "amount_raw is a 128-bit integer, so SUM() is exact. Writes, file "
            "access, and multiple statements are refused."
        )
    )
    def sql(query: str, limit: int = 100) -> dict[str, Any]:
        from ..store.analytics import AnalyticsView

        capped = _cap(limit, config.max_rows)
        # Decided before the view is opened: opening a DuckDB path creates the
        # file, after which "does it exist" answers yes and the build is
        # skipped forever. A view built once and never refreshed serves last
        # week's answer as though it were current, which is the same failure
        # as a cached error -- stale data that looks like data.
        needs_build = config.view is None or not Path(config.view).exists()
        if not needs_build and config.view is not None:
            store_mtime = config.store.stat().st_mtime if config.store.exists() else 0
            needs_build = Path(config.view).stat().st_mtime < store_mtime

        view = AnalyticsView(config.view or ":memory:")
        try:
            if needs_build:
                view.build_from_sqlite(config.store)
            columns = view.columns(query)
            rows = view.sql(query)
        finally:
            view.close()

        return {
            "columns": columns,
            # str() on every cell: a HUGEINT sum is exactly the value that does
            # not survive JSON as a number.
            "rows": [[_cell(v) for v in row] for row in rows[:capped]],
            "shown": min(len(rows), capped),
            "total_rows": len(rows),
            "truncated": len(rows) > capped,
        }

    @server.tool(
        description=(
            "Export the fund-flow graph around an address, with attribution on "
            "every node. Formats: d3, cytoscape, dot, gexf. Nodes marked "
            "frontier were seen but never expanded --- the graph stops there "
            "because nobody looked further, not because nothing is further."
        )
    )
    def export_graph(
        address: str,
        chain: str | None = None,
        direction: str = "out",
        fmt: str = "d3",
        limit: int = 100,
    ) -> dict[str, Any]:
        from ..render.graph import Edge, Graph, Node, to_cytoscape, to_d3, to_dot, to_gexf

        # Validated before touching the store: doing the work and then
        # refusing wastes it, and the error is clearer next to the input.
        writer = {"d3": to_d3, "cytoscape": to_cytoscape, "dot": to_dot, "gexf": to_gexf}
        if fmt not in writer:
            raise AgentError(f"format must be one of {', '.join(sorted(writer))}")
        if direction not in ("out", "in"):
            raise AgentError("direction must be 'out' or 'in'")

        chain_id = _chain(chain) or ChainId.evm(1)
        capped = _cap(limit, config.max_rows)
        store = _store()
        try:
            edges = store.edges(address, chain_id, direction=direction)
            claims = {address: store.attributions(address)}
            graph = Graph(seeds=[f"{chain_id}:{address}"], truncated=len(edges) > capped)
            graph.add_node(
                Node(address=address, chain=str(chain_id), is_seed=True, expanded=True)
            )
            for e in sorted(edges, key=lambda x: x.total_raw, reverse=True)[:capped]:
                other = e.recipient if direction == "out" else e.sender
                if other not in claims:
                    claims[other] = store.attributions(other)
                graph.add_node(Node(address=other, chain=str(chain_id)))
                graph.add_edge(
                    Edge(
                        source=e.sender,
                        target=e.recipient,
                        chain=str(chain_id),
                        symbol=e.asset or "",
                        total_raw=e.total_raw,
                        transfer_count=e.transfer_count,
                        asset=e.asset,
                    )
                )
        finally:
            store.close()

        for addr, found in claims.items():
            for a in found:
                graph.attribute(addr, str(chain_id), a)

        return {
            "format": fmt,
            "chain": str(chain_id),
            "content": writer[fmt](graph),
            "summary": graph.summary(),
        }

    @server.tool(description="What this store contains: counts, chains, and coverage.")
    def store_stats() -> dict[str, Any]:
        store = _store()
        try:
            stats = store.stats()
        finally:
            store.close()
        return {
            "transfers": stats.transfers,
            "addresses": stats.addresses,
            "attributions": stats.attributions,
            "chains": stats.chains,
            "rebuildable_from_cache": stats.rebuildable,
        }

    # ------------------------------------------------------------------ writing

    if config.writable:

        @server.tool(
            description=(
                "Record an attribution for an address. The source is set "
                "automatically to identify this agent, so a human reviewing the "
                "store later can tell a model's suggestion from their own "
                "judgement. A confidence of low or speculative requires a "
                "rationale --- state the actual evidence, not that it seems "
                "likely."
            )
        )
        def label_address(
            address: str,
            label: str,
            category: str = "service",
            confidence: str = "medium",
            rationale: str = "",
            chain: str | None = None,
        ) -> dict[str, Any]:
            try:
                attribution = Attribution(
                    label=label,
                    category=Category(category),
                    confidence=Confidence[confidence.strip().upper()],
                    # INFERENCE, not MANUAL: this did not come from a human
                    # reading evidence, and the method is what a later reader
                    # uses to weigh it.
                    method=Method.INFERENCE,
                    source=f"{AGENT_SOURCE_PREFIX}{config.agent_name}",
                    address=address,
                    chain=_chain(chain),
                    rationale=rationale,
                )
            except (ValueError, KeyError) as exc:
                raise AgentError(str(exc)) from exc

            store = SqliteStore(config.store)
            try:
                store.put_attributions([attribution])
            finally:
                store.close()
            return {
                "recorded": _claim(attribution),
                "note": (
                    "Written as an agent-sourced claim. It sits alongside any "
                    "existing claims rather than replacing them."
                ),
            }

    return server


def _cell(value: Any) -> Any:
    if isinstance(value, int) and abs(value) > 2**53:
        return str(value)
    if isinstance(value, (str, bool, float, type(None))):
        return value
    if isinstance(value, int):
        return value
    return str(value)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="chainscope-mcp", description="Serve chainscope over MCP."
    )
    parser.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    parser.add_argument("--view", type=Path, default=None, help="DuckDB analytical view")
    parser.add_argument(
        "--writable",
        action="store_true",
        help="allow the agent to record labels. Off by default; agent-written "
        "claims are always sourced as such.",
    )
    parser.add_argument("--agent-name", default="unknown-agent")
    parser.add_argument("--max-rows", type=int, default=500)
    args = parser.parse_args(argv)
    if args.max_rows < 1:
        parser.error("--max-rows must be at least 1")

    server = build_server(
        ServerConfig(
            store=args.store,
            view=args.view,
            writable=args.writable,
            agent_name=args.agent_name,
            max_rows=args.max_rows,
        )
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def describe_tools() -> str:
    """The tool list as text, for documentation and for `chainscope doctor`."""
    return json.dumps(
        [
            "resolve_address",
            "flows",
            "search_transfers",
            "sql",
            "export_graph",
            "store_stats",
            "label_address (only with --writable)",
        ],
        indent=2,
    )
