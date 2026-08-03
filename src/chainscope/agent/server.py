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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..analysis.impersonation import report as impersonation_report
from ..analysis.poisoning import DEFAULT_EDGES as POISON_EDGES
from ..analysis.poisoning import chance_of_collision, find_lookalikes
from ..analysis.probing import (
    MIN_ESCALATION_GROWTH,
    MIN_ESCALATION_STEPS,
    detect_probes,
)
from ..analysis.route import find_routes
from ..analysis.taint import TaintPolicy, trace_origins, trace_taint
from ..case.leads import LeadStore, Verdict
from ..chains import address_key, fold_if_hex
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

    case: Path = Path(".chainscope/case.db")
    """The narrative and the correspondence ledger. Separate from the store
    because neither is rebuildable from the cache."""

    writable: bool = False
    """Whether the agent may record labels. Off by default."""

    agent_name: str = "unknown-agent"
    max_rows: int = 500
    """Hard cap on any result. Exceeding it is reported, never silently applied
    --- an agent cannot tell a short list from a truncated one."""

    max_corpus: int = 50_000
    """Transfers a *trace* may read, as distinct from rows it may return.

    These were the same number, and they are different things. `max_rows`
    caps output; a taint trace has to read the transfers the money moved
    through, which is a corpus and not a result. Capping the corpus at 500
    meant the trace read the first five hundred transfers in the store ---
    measured: on a store of six hundred, the theft being traced was **not
    among them**, and the answer came back "nothing tainted".

    That is not a short answer. It is a confident wrong one, produced by
    looking in the wrong place.
    """


def _chain(raw: str | None) -> ChainId | None:
    """Parse a chain id, or refuse. Absence is fine; a typo is not.

    Returning None for an unparsable value meant "bsc" or a mistyped CAIP-2 was
    treated as *unspecified*, which `flows` and `export_graph` then read as
    Ethereum and `search_transfers` read as "every chain". An agent asking
    about one chain would get a confident answer about another.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return ChainId.evm(int(text))
    namespace, _, reference = text.partition(":")
    if not reference:
        raise AgentError(
            f"not a chain id: {raw!r}. Use an EVM chain number (1, 56) or a "
            f"CAIP-2 identifier (eip155:1, sui:mainnet)."
        )
    return ChainId(namespace, reference)


def _address_key(chain: ChainId | None, address: str) -> str:
    """Normalise an address the way its own chain says to.

    `.lower()` is correct on EVM hex and destroys base58 and bech32 --- an
    error that does not raise, it just makes two addresses look like one or one
    look like two. When the caller named a chain the adapter decides; when they
    did not, `fold_if_hex` folds only what is unambiguously 42-character hex and
    leaves everything else exactly as typed.
    """
    raw = address.strip()
    return address_key(chain, raw) if chain is not None else fold_if_hex(raw)


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
            # One past the cap, so "there is more" is a fact rather than an
            # inference -- and so a query returning millions of rows does not
            # materialise all of them first. max_rows capped the response and
            # not the work until this existed.
            rows = view.sql_limited(query, capped + 1)
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

    @server.tool(
        description=(
            "Find probing sequences in an address's outbound transfers from the "
            "store: a small test payment before a much larger one, or a run of "
            "strictly increasing amounts to one destination. Both mark somebody "
            "verifying a route before trusting it, which usually means the first "
            "use of a service. Reads the store only --- no network. Returns an "
            "empty list far more often than not, and that is not evidence the "
            "behaviour is absent."
        )
    )
    def find_probes(
        address: str,
        chain: str | None = None,
        min_steps: int = MIN_ESCALATION_STEPS,
        min_growth: float = MIN_ESCALATION_GROWTH,
        limit: int = 500,
    ) -> dict[str, Any]:
        if not address.strip():
            raise AgentError("address is required")
        capped = _cap(limit, config.max_rows)
        store = _store()
        try:
            rows = list(
                store.transfers(
                    Query(
                        chain=_chain(chain),
                        sender=_address_key(_chain(chain), address),
                        limit=capped,
                    )
                )
            )
        finally:
            store.close()

        probes = detect_probes(rows, min_steps=min_steps, min_growth=min_growth)
        result: dict[str, Any] = {
            "address": _address_key(_chain(chain), address),
            "transfers_examined": len(rows),
            "probes": [p.to_dict() for p in probes],
        }
        if len(rows) >= capped:
            # A probe is a sequence, so a clipped window shortens runs and can
            # push a real one below the floor without anything looking wrong.
            result["truncated"] = (
                f"only the first {capped} transfers were read; a sequence "
                f"beginning earlier is cut short and may not appear at all"
            )
        if not probes:
            result["note"] = (
                "No probing sequence found. This is the common result and is not "
                "evidence of absence: a probe split across two addresses, or "
                "paced beyond what the store holds, leaves no run to find."
            )
        return result

    @server.tool(
        description=(
            "Check which assets an address touched are impersonating other "
            "assets. Reads the store only --- no network. Answer this BEFORE "
            "quoting any per-symbol total: measured on a real case, 42 of 55 "
            "ERC-20 transfers to one address belonged to tokens imitating USDC "
            "and ETH, so a total grouped by symbol was mostly forgery and "
            "looked exactly like a real number. Three mechanisms are checked "
            "and no two overlap --- a symbol with a foreign letter spliced in "
            # The examples are the point of the description: an agent reading it
            # needs to see what a forgery looks like. Marked per line so the
            # rest of this large file keeps the check.
            "(UЅDC), a symbol written entirely in another script (ЕТН), and a "  # noqa: RUF001
            "plain-ASCII token simply named after a real one (ETH), which only "
            "the contract address can catch. Reports both the forgeries and "
            "the genuine assets, and never filters anything out."
        )
    )
    def check_impersonation(
        address: str,
        chain: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if not address.strip():
            raise AgentError("address is required")
        capped = _cap(limit, config.max_rows)
        where = _chain(chain)
        # `address_key`, never `.lower()`. Folding case is right on EVM hex and
        # destroys a base58 or bech32 address --- and this tool serves every
        # chain the store holds. The same mistake was driven out of 27 sites
        # earlier and came straight back in here, because the ratchet's pattern
        # did not match `address.strip().lower()`.
        key = _address_key(where, address)
        store = _store()
        try:
            # Both directions: a poisoning token is *sent to* the subject and
            # never sent by it, so an outbound-only read misses the attack.
            rows = list(store.transfers(Query(chain=where, address=key, limit=capped)))
        finally:
            store.close()

        rep = impersonation_report(rows, where)
        result: dict[str, Any] = {
            "address": key,
            "transfers_examined": len(rows),
            "summary": rep.summary(),
            "forged_transfers": rep.forged_transfers,
            "total_transfers": rep.total_transfers,
            "assets": [
                {
                    "symbol": a.symbol,
                    "contract": a.contract,
                    "verdict": a.verdict,
                    "transfers": a.transfers,
                    "resembles": a.resembles or None,
                    "reasons": list(a.reasons),
                    # Codepoint and Unicode name per character, so the caller
                    # can verify the claim rather than take it on trust.
                    "characters": [list(c) for c in a.characters],
                }
                for a in rep.assets
            ],
        }
        if len(rows) >= capped:
            result["truncated"] = (
                f"only the first {capped} transfers were read; an asset appearing "
                f"only after that point was not inspected, so the share is over "
                f"what was read, not over what exists"
            )
        result["caveat"] = (
            "'unlisted' is not a clean bill. Most tokens are in no registry and "
            "are entirely real --- it means the canonical check had nothing to "
            "compare against. Only 'genuine' is a positive statement."
        )
        return result

    @server.tool(
        description=(
            "Check whether any of an address's counterparties were generated to "
            "be mistaken for another one --- address poisoning. Reads the store "
            "only. An attacker grinds an address matching the first four and "
            "last four characters of a real counterparty, sends a zero-value "
            "transfer so it appears in the history, and waits for somebody to "
            "copy it. Reports the coincidence probability, which is the point: "
            "4+4 hex is 32 bits, so a collision among a few dozen addresses is "
            "about 1e-7 and several collisions are proof of grinding. It will "
            "often say it CANNOT TELL which address is the real one --- respect "
            "that, because naming the wrong one is how the next payment reaches "
            "the attacker."
        )
    )
    def check_address_poisoning(
        address: str,
        chain: str | None = None,
        edges: int = POISON_EDGES,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if not address.strip():
            raise AgentError("address is required")
        capped = _cap(limit, config.max_rows)
        where = _chain(chain)
        subject = _address_key(where, address)
        store = _store()
        try:
            rows = list(store.transfers(Query(chain=where, address=subject, limit=capped)))
        finally:
            store.close()

        groups, examined = find_lookalikes(rows, subject, edges=edges, chain=where)
        result: dict[str, Any] = {
            "address": subject,
            "counterparties_examined": examined,
            "transfers_examined": len(rows),
            "chance_of_one_collision_by_coincidence": chance_of_collision(examined, edges),
            "groups": [
                {
                    "looks_like": f"0x{g.prefix}\u2026{g.suffix}",
                    "explanation": g.describe(),
                    "decidable": g.is_decidable,
                    "paid_by_subject": [m.address for m in g.paid],
                    "suspects": [m.address for m in g.suspects],
                    "members": [
                        {
                            "address": m.address,
                            "paid_by_subject": m.sent_to_by_subject,
                            "claimed_paid_by_forged_token": m.sent_untrusted,
                            "received_from": m.received_from,
                            "zero_value": m.zero_value,
                            "transfers": m.total_transfers,
                        }
                        for m in g.members
                    ],
                }
                for g in groups
            ],
        }
        if len(rows) >= capped:
            result["truncated"] = (
                f"only the first {capped} transfers were read; a lookalike pair "
                f"whose second member falls outside that window does not appear"
            )
        if not groups:
            result["note"] = (
                "No lookalike group found in what was read. Not a guarantee: an "
                "address poisoned outside this window leaves nothing here."
            )
        return result

    @server.tool(
        description=(
            "Find how money could have travelled from one address to another, "
            "using the store. THE routing question --- 'how did A get to B'. "
            "Every route returned is time-respecting: its timestamps do not "
            "decrease, so it is a way the money could have moved in the order "
            "things actually happened. That matters more than it sounds: "
            "measured on a real ledger, 62% of the shortest paths a plain "
            "hop-count search returns are causally impossible, with a hop "
            "occurring before the money arrived. Routes crossing a high-degree "
            "address are withheld by default and counted, because a custodian "
            "commingles what it receives --- such a route is a link in the "
            "ledger, not in the money. Each route reports what its narrowest "
            "hop could carry. A route is a CANDIDATE, not proof; and no route "
            "found is not proof of no connection."
        )
    )
    def find_route(
        source: str,
        target: str,
        chain: str | None = None,
        max_hops: int = 5,
        allow_hubs: bool = False,
        limit: int = 5000,
    ) -> dict[str, Any]:
        if not source.strip() or not target.strip():
            raise AgentError("both source and target are required")
        # `max_corpus`, not `max_rows`. This reads the whole store rather than
        # one address's rows, which is what the other corpus-wide tools do ---
        # capping it at the per-address limit silently searched 500 transfers
        # and reported "no route" over a store holding far more.
        capped = _cap(limit, config.max_corpus)
        where = _chain(chain)
        src = _address_key(where, source)
        dst = _address_key(where, target)
        store = _store()
        try:
            rows = list(store.transfers(Query(chain=where, limit=capped)))
        finally:
            store.close()

        routes, notes = find_routes(
            rows, src, dst, max_hops=max_hops, allow_hubs=allow_hubs, chain=where
        )
        result: dict[str, Any] = {
            "source": src,
            "target": dst,
            "transfers_searched": len(rows),
            "routes": [
                {
                    "addresses": r.addresses,
                    "hops": r.length,
                    "carries_at_most": r.carries,
                    "same_asset_throughout": r.single_asset,
                    "crosses_hub": r.crosses_hub or None,
                    "explanation": r.describe(),
                    "transactions": [h.tx for h in r.hops if h.tx],
                }
                for r in routes
            ],
            **notes,
        }
        if len(rows) >= capped:
            result["truncated"] = (
                f"only {capped} transfers were searched; a route through a hop "
                f"outside that set will not appear"
            )
        if not routes:
            result["note"] = (
                "No time-respecting route within the store. NOT proof the two are "
                "unconnected: funds that crossed a chain, an exchange, or an "
                "address the store has never seen leave no such chain behind."
            )
        return result

    @server.tool(
        description=(
            "Trace how much of each address's holdings came from a source "
            "address, using the store. Answers 'how much of this balance is "
            "stolen', which is different from 'is this reachable from the "
            "theft' --- after a few hops almost everything is reachable. "
            "Defaults to FIFO (Clayton's Case, 1816), the only rule of the "
            "three that conserves the stolen amount. The reply separates "
            "addresses that HOLD tainted value from those it merely passed "
            "through; those are different claims."
        )
    )
    def trace_stolen_funds(
        source: str,
        amount: str | None = None,
        chain: str | None = None,
        policy: str = "fifo",
        limit: int = 5000,
    ) -> dict[str, Any]:
        if not source.strip():
            raise AgentError("source address is required")
        try:
            rule = TaintPolicy(policy)
        except ValueError as exc:
            raise AgentError(
                f"policy must be one of {', '.join(p.value for p in TaintPolicy)}"
            ) from exc

        capped = _cap(limit, config.max_corpus)
        store = _store()
        chain_id = _chain(chain)
        seed = address_key(chain_id, source)
        try:
            # Ordered by time, because FIFO depends on arrival order --- an
            # unordered corpus changes which funds paid for what.
            rows = list(store.transfers(Query(chain=chain_id, limit=capped, order="time")))
            reachable = store.count(Query(chain=chain_id, address=seed))
        finally:
            store.close()

        if not reachable:
            raise AgentError(
                f"{source} appears in no transfer in this store. That is not "
                f"'nothing was traced' --- it is 'the trace never had anything to "
                f"start from'. Ingest the source's history first."
            )
        if not any(
            (t.sender and t.sender.key == seed) or (t.recipient and t.recipient.key == seed)
            for t in rows
        ):
            # The source exists but fell outside the corpus that was read.
            # Reporting an empty trace here would be the confident wrong answer.
            raise AgentError(
                f"{source} is in this store but not in the {len(rows)} transfers "
                f"read for this trace. Narrow the chain, or raise the corpus "
                f"limit --- an empty result here would mean 'not looked at', not "
                f"'not found'."
            )

        try:
            sources: Any = {seed: int(amount)} if amount else {seed}
        except ValueError as exc:
            raise AgentError(
                f"amount is a raw integer string in the asset's smallest unit: {exc}"
            ) from exc

        result = trace_taint(rows, sources, policy=rule)
        out = result.to_dict()
        out["source"] = seed
        out["transfers_examined"] = len(rows)
        # Held and touched are reported separately on purpose, and named so an
        # agent summarising this cannot merge them.
        out["passed_through_but_holds_none"] = sorted(result.touched - set(result.tainted))
        if len(rows) >= capped:
            out["truncated"] = (
                f"only {capped} transfers were read, and FIFO depends on arrival "
                f"order --- a clipped window changes which funds paid for what, "
                f"not merely how far the trace reached"
            )
        if rule is not TaintPolicy.FIFO:
            out["policy_warning"] = (
                "haircut loses value it cannot recover and poison invents value "
                "never stolen. Only FIFO conserves the amount; the others are "
                "here for comparison."
            )
        return out

    @server.tool(
        description=(
            "Ask what funded an address's balance --- FIFO run backwards. This "
            "is the question you have when you start from a suspect rather "
            "than from an incident, and it is the half `trace_stolen_funds` "
            "does not answer. `amount` limits it to the most recent N base "
            "units, which is how you ask about the last ten ETH rather than "
            "everything. Origins are immediate senders as far back as the "
            "store reaches; it does not follow further, because each hop back "
            "multiplies the addresses involved."
        )
    )
    def trace_origins_of(
        address: str,
        amount: str | None = None,
        asset: str | None = None,
        chain: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        if not address.strip():
            raise AgentError("address is required")
        capped = _cap(limit, config.max_corpus)
        store = _store()
        chain_id = _chain(chain)
        key = address_key(chain_id, address)
        try:
            rows = list(store.transfers(Query(chain=chain_id, limit=capped, order="time")))
            reachable = store.count(Query(chain=chain_id, address=key))
        finally:
            store.close()

        if not reachable:
            raise AgentError(
                f"{address} appears in no transfer in this store. Reverse tracing "
                f"would return 'no origins', which reads as a finding about the "
                f"balance rather than about the corpus."
            )

        # `address_key`, not `.lower()`: base58 is case-sensitive, so lowering a
        # Solana or Bitcoin address asks about a different account.
        target: Any = (key, asset.lower()) if asset else key
        try:
            want = int(amount) if amount else None
        except ValueError as exc:
            raise AgentError(
                f"amount is a raw integer string in the asset's smallest unit: {exc}"
            ) from exc

        origins = trace_origins(rows, target, amount=want)
        out: dict[str, Any] = {
            "address": key,
            "asset": asset,
            "transfers_examined": len(rows),
            # Strings: base units are wei-scale and do not survive JSON as
            # numbers.
            "origins": {
                (o if isinstance(o, str) else o[0]): str(v)
                for o, v in sorted(origins.items(), key=lambda kv: -kv[1])
            },
        }
        if not origins:
            out["note"] = (
                "Nothing in this store funded that balance. That is not the same "
                "as the address having no history --- it may have been funded "
                "outside what has been collected."
            )
        if len(rows) >= capped:
            out["truncated"] = (
                f"only {capped} transfers were read, and FIFO depends on arrival "
                f"order --- a clipped window changes which lot funded what"
            )
        return out

    @server.tool(
        description=(
            "What this case still does not know: unanswered questions, requests "
            "sent to exchanges that have had no reply, and the reasoning recorded "
            "so far. Read this before summarising a case --- a record listing "
            "only conclusions reads as finished no matter how much of it is not."
        )
    )
    def case_record(limit: int = 50) -> dict[str, Any]:
        from ..case.correspondence import Ledger
        from ..case.log import CaseLog

        capped = _cap(limit, config.max_rows)
        log = CaseLog(config.case)
        try:
            notes = log.notes()
            open_questions = log.open_questions()
            replaced = log.superseded()
        finally:
            log.close()

        ledger = Ledger(config.case)
        try:
            outstanding = ledger.requests(open_only=True)
        finally:
            ledger.close()

        now = datetime.now(timezone.utc)
        out: dict[str, Any] = {
            "open_questions": [
                {"id": n.id, "asked": n.body, "by": n.analyst} for n in open_questions[:capped]
            ],
            "awaiting_reply": [
                {
                    "id": r.id,
                    "counterparty": r.counterparty,
                    "asked_for": r.kind.value,
                    "days_open": r.age_days(now),
                    "overdue": r.overdue_at(now),
                    "about": r.subject or None,
                }
                for r in outstanding[:capped]
            ],
            "notes": [
                {
                    "id": n.id,
                    "kind": n.kind.value,
                    "body": n.body,
                    "by": n.analyst,
                    "at": n.at.isoformat(),
                    "about": n.subject or None,
                    # Kept and marked rather than filtered: what somebody
                    # believed and when it changed is the record.
                    "superseded": n.id in replaced,
                }
                for n in notes[-capped:]
            ],
        }
        if not notes and not outstanding:
            out["note"] = (
                "Nothing is recorded for this case. That means nobody has written "
                "anything down, not that there is nothing outstanding."
            )
        if outstanding:
            out["reading_this"] = (
                "A request with no reply is not a request that was refused. Only a "
                "refusal is a decision somebody made, and only a refusal can be "
                "escalated against --- do not report silence as a denial."
            )
        # Per field, not one flag. "Something was clipped" leaves an agent
        # unable to tell a complete list of open questions from a clipped one,
        # and the open questions are the field this tool exists to return.
        clipped = {
            name: f"{capped} of {total} shown"
            for name, total in (
                ("open_questions", len(open_questions)),
                ("awaiting_reply", len(outstanding)),
                ("notes", len(notes)),
            )
            if total > capped
        }
        if clipped:
            out["truncated"] = clipped
        return out

    @server.tool(
        description=(
            "List the leads recorded for this case --- places to look next, kept "
            "strictly apart from anything concluded. A lead is NOT an "
            "attribution: an ENS text record saying com.twitter=alice means "
            "whoever controls that name typed 'alice' into a field, not that "
            "the address belongs to @alice. Every lead carries the specific "
            "step that would confirm it, and that step is always something a "
            "PERSON does somewhere this tool cannot reach --- do not claim to "
            "have verified one. Settled leads are returned too, including "
            "refuted ones: the record that somebody already checked is what "
            "stops the work being repeated, so read the reason before "
            "suggesting a lead be chased."
        )
    )
    def case_leads(
        address: str | None = None,
        only_open: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        # No caller-chosen path. `case` was a parameter, which let anything
        # talking to this server name an arbitrary file on disk and have it
        # opened as a SQLite database. The server is configured with one case
        # for a reason, and the two write tools beside this never offered it.
        capped = _cap(limit, config.max_rows)
        store = LeadStore(config.case)
        try:
            # Capped in SQL, and counted separately, so a large case does not
            # build every record in order to discard most of them.
            records = (
                store.open_leads(address, capped)
                if only_open
                else store.leads(address, limit=capped)
            )
            total = store.count(address, Verdict.OPEN if only_open else None)
            counts = store.summary()
        finally:
            store.close()
        out: dict[str, Any] = {
            "leads": [
                {
                    "id": r.id,
                    "address": r.address,
                    "kind": r.kind,
                    "value": r.value,
                    "source": r.source,
                    "asserted_by": r.asserted_by,
                    "verify_by": r.verify_by,
                    "verdict": r.verdict.value,
                    "reason": r.reason or None,
                    "settled_by": r.settled_by or None,
                }
                for r in records
            ],
            "counts": counts,
            "caveat": (
                "An open lead is unverified by definition and is not a finding. "
                "A refuted one is finished work --- somebody looked and it was "
                "not there."
            ),
        }
        if total > capped:
            # An agent cannot tell a short list from a truncated one, and a
            # summary written over the first 200 of 500 reads as complete.
            out["truncated"] = (
                f"{total} leads exist; the first {capped} are returned. Raise "
                f"limit or filter by address rather than summarising these as all."
            )
        return out

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

        @server.tool(
            description=(
                "Write down reasoning about this case: an observation, a "
                "decision and why it was taken, or a question nothing has "
                "answered yet. Append-only --- to withdraw an earlier note, "
                "record a correction naming its id rather than expecting an "
                "edit. Authorship is recorded as this agent, so a human can "
                "later tell a model's reasoning from their own."
            )
        )
        def record_note(
            kind: str,
            body: str,
            about: str = "",
            supersedes: int | None = None,
        ) -> dict[str, Any]:
            from ..case.log import CaseLog, Note, NoteKind

            try:
                note = Note(
                    at=datetime.now(timezone.utc),
                    analyst=f"{AGENT_SOURCE_PREFIX}{config.agent_name}",
                    # Not "env"/"git"/"os": none of those is what happened, and
                    # a report has to be able to show that a model wrote this.
                    identified_by="agent",
                    kind=NoteKind(kind.strip().lower()),
                    body=body,
                    subject=about,
                    supersedes=supersedes,
                )
            except ValueError as exc:
                raise AgentError(str(exc)) from exc

            log = CaseLog(config.case)
            try:
                note_id = log.add(note)
            except ValueError as exc:
                raise AgentError(str(exc)) from exc
            finally:
                log.close()
            return {
                "recorded": {"id": note_id, "kind": note.kind.value},
                "note": (
                    "Written as an agent-authored note. It sits in the same "
                    "narrative as a person's and is marked as this agent's."
                ),
            }

        @server.tool(
            description=(
                "File a lead: somewhere to look next. NOT an attribution --- use "
                "label_address for a claim about what an address IS. A lead is "
                "something self-asserted by whoever wrote the source, and "
                "verify_by must name the specific thing that would confirm it, "
                "which is always something a person does off-chain. Filing the "
                "same lead twice returns the existing one and says so; if it has "
                "already been settled, read the reason instead of chasing it."
            )
        )
        def record_lead(
            address: str,
            kind: str,
            value: str,
            source: str,
            verify_by: str,
            asserted_by: str = "",
            chain: str | None = None,
        ) -> dict[str, Any]:
            from ..osint.leads import Lead

            try:
                lead = Lead(
                    address=address,
                    kind=kind,
                    value=value,
                    source=source,
                    asserted_by=asserted_by or "whoever wrote the source",
                    verify_by=verify_by,
                )
            except ValueError as exc:
                raise AgentError(str(exc)) from None

            store = LeadStore(config.case)
            try:
                lead_id, was_new = store.add(lead, config.agent_name, chain)
                record = store.get(lead_id)
            finally:
                store.close()
            return {
                "id": lead_id,
                "new": was_new,
                "verdict": record.verdict.value,
                "reason": record.reason or None,
                "note": (
                    "Recorded as a place to look. It is not evidence of anything."
                    if was_new
                    else f"Already on file, currently {record.verdict.value}."
                ),
            }

        @server.tool(
            description=(
                "Record what checking a lead found. A reason is REQUIRED and "
                "must state what was actually observed --- 'confirmed' with no "
                "basis reads, once you are gone, exactly like a guess. Use "
                "'unreachable' when the check could not be completed (account "
                "deleted, site gone): that is different from 'refuted', which "
                "says the claim is false. Nothing is deleted; the record that "
                "somebody looked is the most useful thing here."
            )
        )
        def settle_lead(lead_id: int, verdict: str, reason: str) -> dict[str, Any]:
            try:
                chosen = Verdict(verdict)
            except ValueError:
                raise AgentError(
                    "verdict must be one of: "
                    + ", ".join(v.value for v in Verdict if v is not Verdict.OPEN)
                ) from None
            if chosen is Verdict.OPEN:
                raise AgentError(
                    "a lead cannot be settled as 'open'. To reopen one, record "
                    "the correction as a note --- the record of who checked and "
                    "what they found is not overwritten"
                )
            if not reason.strip():
                # Checked here as well as in the store. The store's refusal is
                # the guarantee; this one makes the message the agent sees name
                # the argument it got wrong, rather than surfacing a storage
                # error for what is an input mistake.
                raise AgentError(
                    "a reason is required. State what you actually observed --- "
                    "'confirmed' with no basis reads, once you are gone, exactly "
                    "like a guess"
                )
            store = LeadStore(config.case)
            try:
                record = store.settle(lead_id, chosen, reason, config.agent_name)
            except ValueError as exc:
                raise AgentError(str(exc)) from None
            finally:
                store.close()
            return {
                "id": record.id,
                "verdict": record.verdict.value,
                "reason": record.reason,
                "settled_by": record.settled_by,
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
        "--case",
        type=Path,
        default=Path(".chainscope/case.db"),
        help="the case narrative and correspondence ledger",
    )
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
            case=args.case,
            writable=args.writable,
            agent_name=args.agent_name,
            max_rows=args.max_rows,
        )
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


#: Tools this server exposes. Hand-kept, and checked against the source by
#: `tests/unit/test_agent_tools_are_listed.py` --- the list went three tools
#: stale unnoticed, which is the failure this project keeps finding: a
#: capability that exists and no surface admits to.
#:
#: It cannot be derived at runtime. `doctor` calls this to say what an agent
#: would get, and the MCP SDK is an optional extra --- building a server to ask
#: it would make that answer unavailable on exactly the install that needs it.
TOOLS = (
    "resolve_address",
    "flows",
    "search_transfers",
    "sql",
    "export_graph",
    "store_stats",
    "find_probes",
    "check_impersonation",
    "check_address_poisoning",
    "find_route",
    "trace_stolen_funds",
    "trace_origins_of",
    "case_record",
    "case_leads",
    "record_lead (only with --writable)",
    "settle_lead (only with --writable)",
    "label_address (only with --writable)",
    "record_note (only with --writable)",
)


def describe_tools() -> str:
    """The tool list as text, for documentation and for `chainscope doctor`."""
    return json.dumps(list(TOOLS), indent=2)
