"""A localhost API, for the browser extension and anything else on this machine.

The extension needs to ask "what do we know about this address" while you are
looking at it, and to record a label without leaving the page. Neither is
possible over the MCP transport, which speaks stdio to one client.

**The security model is the interesting part, and getting it wrong here is
worse than not shipping it.** A server bound to localhost is reachable by every
page in the browser: `fetch("http://127.0.0.1:8787/...")` from any tab, and the
browser will happily make the request. Binding to loopback keeps other machines
out and does nothing about the machine you are on.

So three things, and none of them is optional:

*A bearer token, generated per run.* Printed once at startup for the extension
to be configured with. Without it every request is refused, including reads:
your label set is an investigation artefact, and which addresses you have been
looking at is itself sensitive.

*No credentials in CORS.* ``Access-Control-Allow-Origin`` is echoed only for
configured origins and ``Allow-Credentials`` is never sent, so a hostile page
cannot ride an ambient session --- there is none to ride.

*Writes are opt-in.* ``--writable`` is off by default. A read-only server that
leaks nothing is a much smaller thing to get wrong.

Built on :mod:`http.server`. It is not a production web server and does not
need to be: one client, one machine, a handful of requests while somebody reads
a page. Adding a framework here would be a dependency and a build step in
exchange for concurrency nobody needs.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..providers.base import Capability, ProviderError, ResultTruncated
from ..store.sqlite import SqliteStore
from .webapp import page as _page

__all__ = ["LocalServer", "ServerOptions", "main"]

#: Body larger than this is refused unread. Nothing this API accepts is big,
#: and an unbounded read from a socket anyone on the machine can open is a
#: memory-exhaustion primitive.
MAX_BODY = 64 * 1024

#: Origins the extension may run from. Anything else gets no CORS headers, so
#: the browser refuses to hand the response to the page.
DEFAULT_ORIGINS = ("chrome-extension://", "moz-extension://")


def origin_allowed(origin: str, allowed: Sequence[str]) -> str | None:
    """Match an ``Origin`` header against the configured list.

    Two rules, because the configured values come in two shapes.

    A bare scheme --- ``chrome-extension://`` --- is a *prefix* on purpose:
    every extension has its own id and nobody is going to enumerate them, and
    an attacker cannot register an extension id under someone else's scheme.

    Anything naming a host is matched **exactly**. Prefix-matching those meant
    ``https://etherscan.io`` also admitted ``https://etherscan.io.evil.com``,
    which is a domain anybody can register --- and the reply carries
    ``Access-Control-Allow-Origin`` for it, so the page can then read the
    responses.

    A module-level function rather than a method so it can be tested without
    binding a socket; the socket tests carry the ``network`` marker and are
    deselected by default, which is a poor place for the rule that decides who
    may read the label store.
    """
    if not origin:
        return None
    for entry in allowed:
        if entry.endswith("://"):
            if origin.startswith(entry) and len(origin) > len(entry):
                return origin
        elif origin == entry:
            return origin
    return None


@dataclass
class ServerOptions:
    store: Path = Path(".chainscope/store.db")
    host: str = "127.0.0.1"
    """Loopback. Binding wider would expose an unauthenticated-by-default
    label store to the network; the token is the second lock, not the first."""

    port: int = 8787
    token: str = ""

    analyst: str = ""
    """Who is tagging from the browser.

    The *request* cannot supply this, for the same reason it cannot supply
    `source`: any page in the browser can reach this endpoint, and a claim that
    picks its own authorship is worse than one carrying none. But the person
    running this server is identifiable --- their machine, their loopback, their
    token --- so it is taken at startup and applied to everything written here.

    Empty means nobody was recorded, which is what an OS account yields: a
    machine login is not authorship, and `report` says "no analyst recorded"
    rather than signing somebody's name to it.
    """
    writable: bool = False
    origins: tuple[str, ...] = DEFAULT_ORIGINS
    agent_name: str = "browser-extension"

    def __post_init__(self) -> None:
        if not self.token:
            # 32 bytes of urandom. Regenerated per run, so a token pasted into
            # an extension once does not outlive the session that issued it.
            self.token = secrets.token_urlsafe(32)


def _first(query: dict[str, list[str]], key: str) -> str | None:
    """First value for a query parameter, or None."""
    values = query.get(key)
    return values[0] if values else None


@dataclass
class _Handlers:
    """Request handling, kept out of the BaseHTTPRequestHandler subclass.

    That class is instantiated per request and is an awkward place to hold
    configuration; this is a plain object the handler consults.
    """

    options: ServerOptions
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------ store

    #: The resolver, once built. See `_from_sources`.
    _resolver: Any = None

    def _store(self) -> SqliteStore:
        if not self.options.store.exists():
            raise FileNotFoundError(
                f"no store at {self.options.store}. Run an analysis first, or "
                f"point --store at an existing one."
            )
        return SqliteStore(self.options.store)

    @staticmethod
    def _chain(raw: str | None) -> ChainId | None:
        if not raw:
            return None
        text = raw.strip()
        if text.isdigit():
            return ChainId.evm(int(text))
        namespace, _, reference = text.partition(":")
        if not reference:
            # Refused rather than treated as unspecified: downstream, "no
            # chain" means "every chain", and a typo would silently widen the
            # query instead of narrowing it.
            raise ValueError(f"not a chain id: {raw!r}. Use an EVM chain number or CAIP-2.")
        return ChainId(namespace, reference)

    # ------------------------------------------------------------------ routes

    def resolve(self, query: dict[str, list[str]]) -> dict[str, Any]:
        address = (_first(query, "address") or "").strip()
        if not address:
            raise ValueError("address is required")
        chain = self._chain(_first(query, "chain"))

        # Through the resolver, not straight to the store. Reading the store
        # alone meant every configured source --- sanctions, the scam list, the
        # 17,000-address label dump --- was invisible to the page, which
        # therefore showed `unlabelled` for addresses the tool could name. The
        # store holds what somebody *wrote*; the sources hold what is known.
        claims, unreachable = self._from_sources(address, chain)
        store = self._store()
        try:
            claims += [
                c
                for c in store.attributions(address)
                # Chain-agnostic claims apply everywhere --- that is how
                # sanctions lists are published. Other-chain claims do not: the
                # same twenty bytes exist on every EVM network.
                if chain is None or c.chain is None or c.chain == chain
            ]
        finally:
            store.close()

        return {
            "address": address,
            "chain": str(chain) if chain else None,
            "claims": [
                {
                    "label": c.label,
                    "category": c.category.value,
                    "confidence": c.confidence.name,
                    "confidence_value": int(c.confidence),
                    "method": c.method.value,
                    "source": c.source,
                    "rationale": c.rationale,
                    "chain": str(c.chain) if c.chain else None,
                }
                for c in claims
            ],
            # Named, not implied by an empty claim list. A source that could
            # not be read produces the same empty list as an address nobody has
            # ever labelled, and only one of those two is a finding.
            "unreachable_sources": unreachable,
            "reliable": not unreachable,
            "note": _note(bool(claims), unreachable),
        }

    def _from_sources(self, address: str, chain: ChainId | None) -> tuple[list[Any], list[str]]:
        """Claims, and the sources that could not answer.

        Returns both, and the second is the point. A failure is swallowed *per
        source* so one missing data file does not blank the panel --- but a
        swallowed failure that is never reported arrives on screen as "no
        attribution", in the same words an honest empty result uses.

        This docstring used to claim the caller reported it. It did not:
        `_claims` discarded `Resolution.failed` and nothing in the response
        carried it. The fifth time in this codebase that prose described a
        property the code lacked, and the same shape every time --- an absence
        rendered as a result.

        The resolver is built once. Constructing it per call rebuilt every
        source for every node, including the contract registry's
        quarter-million filename index: 8.7 seconds for a twenty-node graph
        against 0.51 cached. That fix was lost when this method was rewritten
        to use `resolver_for` and is restored here.
        """
        from ..attribution.build import resolver_for

        if self._resolver is None:
            base = self.options.store.parent.parent / "data" / "labels"
            self._resolver = resolver_for(base)
        return self._claims(self._resolver, address, chain)

    @staticmethod
    def _claims(
        resolver: Any, address: str, chain: ChainId | None
    ) -> tuple[list[Any], list[str]]:
        if not resolver.sources:
            return [], ["no attribution source is configured, so nothing was consulted"]
        found = resolver.resolve(address, chain)
        failed = [f"{name}: {why}" for name, why in found.failed]
        claims = list(found.entity.all_claims) if found.entity else []
        return claims, failed

    def flows(self, query: dict[str, list[str]]) -> dict[str, Any]:
        address = (_first(query, "address") or "").strip()
        if not address:
            raise ValueError("address is required")
        chain = self._chain(_first(query, "chain") or "1") or ChainId.evm(1)
        direction = _first(query, "direction") or "out"
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        limit = max(1, min(int(_first(query, "limit") or "25"), 200))

        store = self._store()
        try:
            edges = store.edges(address, chain, direction=direction)
        finally:
            store.close()
        edges.sort(key=lambda e: e.total_raw, reverse=True)

        return {
            "address": address,
            "chain": str(chain),
            "direction": direction,
            "flows": [
                {
                    "counterparty": e.recipient if direction == "out" else e.sender,
                    # A string: these exceed what a JSON number holds exactly,
                    # and the extension renders them with integer arithmetic.
                    "total_raw": str(e.total_raw),
                    "symbol": e.symbol,
                    "decimals": e.decimals,
                    "transfers": e.transfer_count,
                }
                for e in edges[:limit]
            ],
            "shown": min(len(edges), limit),
            "total_available": len(edges),
            "truncated": len(edges) > limit,
        }

    def tag(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.options.writable:
            raise PermissionError(
                "this server is read-only. Restart with --writable to record labels."
            )
        address = str(body.get("address", "")).strip()
        label = str(body.get("label", "")).strip()
        if not address or not label:
            raise ValueError("address and label are required")

        # The origin marker is not optional and cannot be replaced.
        #
        # Caller-supplied text used to *become* the source when present, so a
        # claim written through this endpoint could be labelled "OFAC SDN list"
        # and would sit in the store indistinguishable from a real import of
        # one. This endpoint is reachable by any page in the user's browser;
        # letting a request choose its own provenance defeats the property the
        # whole attribution type exists to guarantee.
        #
        # Caller text is kept --- it is usually the useful part, "etherscan
        # public tag" or a case reference --- but only ever appended, so the
        # record always says a browser wrote it and what it claimed to be
        # reading.
        origin = f"browser:{self.options.agent_name}"
        supplied = str(body.get("source", "")).strip()
        source = f"{origin} (reported: {supplied})" if supplied else origin
        try:
            attribution = Attribution(
                label=label,
                category=Category(str(body.get("category", "service"))),
                confidence=Confidence[str(body.get("confidence", "medium")).upper()],
                # MANUAL: a person clicked this while reading the page. That is
                # exactly the judgement the method field is meant to record.
                method=Method.MANUAL,
                source=source,
                address=address,
                chain=self._chain(body.get("chain")),
                rationale=str(body.get("rationale", "")),
                # From the server, never the request --- the same reasoning as
                # `source` above. A browser-written claim carried no analyst, so
                # `report` filed a label a person had typed alongside bulk
                # imports and heuristics.
                analyst=self.options.analyst,
            )
        except KeyError as exc:
            raise ValueError(f"unknown confidence: {exc}") from exc

        # Serialised: two rapid clicks from the extension would otherwise open
        # two connections to the same SQLite file.
        with self._lock:
            store = SqliteStore(self.options.store)
            try:
                store.put_attributions([attribution])
            finally:
                store.close()

        return {
            "recorded": {
                "address": address,
                "label": attribution.label,
                "category": attribution.category.value,
                "confidence": attribution.confidence.name,
                "source": attribution.source,
            }
        }

    # ------------------------------------------------------- the browsable UI

    def graph(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """A flow graph around a seed, built from the store alone.

        No network. An address the store has never seen is reported as exactly
        that rather than drawn as an empty graph --- the two look identical on
        screen and mean opposite things, and the second is the one that quietly
        ends an investigation.
        """
        from ..cli.commands.graph import _attribute, _walk
        from ..render.flow import layer_nodes

        address = (_first(query, "address") or "").strip()
        if not address:
            raise ValueError("address is required")
        chain = self._chain(_first(query, "chain") or "1") or ChainId.evm(1)
        depth = max(1, min(int(_first(query, "depth") or "3"), 6))
        max_nodes = max(2, min(int(_first(query, "max_nodes") or "60"), 400))

        fetched, complete = 0, True
        # `Node` is frozen, so source labels are collected beside the graph
        # rather than written into it --- and the payload prefers the store's
        # label, because a claim somebody recorded outranks one a list asserts.
        from_sources: dict[str, tuple[str, str]] = {}
        store = self._store()
        try:
            if not store.edges(address, chain, direction="out") and not store.edges(
                address, chain, direction="in"
            ):
                # Fetch it. This used to refuse, on the grounds that spending
                # somebody's rate limit is a decision --- which is true, and the
                # refusal still made the tool useless the first time anybody
                # typed an address into it. The honest form of that principle is
                # to *say* the network was used, not to withhold the feature:
                # `fetched` travels back and the status line reports it.
                fetched, complete = _fetch_into(store, address, chain)
                if not fetched:
                    raise ValueError(
                        f"{address} has no transfers on {chain} --- not in the "
                        f"store, and the providers returned none either. That is "
                        f"an answer about the address rather than about the store"
                    )
            built = _walk(
                store,
                address,
                chain,
                depth=depth,
                max_nodes=max_nodes,
                per_node=12,
                direction="both",
            )
            for node in list(built.nodes.values()):
                _attribute(store, built, node.address, chain)
                # And from the sources. `_attribute` reads the store, which
                # holds what somebody wrote --- the sources hold what is known,
                # and a page showing `unlabelled` for an address the tool can
                # name is the gap this whole source was added to close.
                if not node.label:
                    node_claims, _ = self._from_sources(node.address, chain)
                    for claim in node_claims:
                        from_sources[node.address] = (
                            claim.label,
                            claim.category.value,
                        )
                        break
        finally:
            store.close()

        # Hop distance from the seed, computed the same way the flow renderer
        # does it: breadth-first over directed edges, so a column means "how
        # many hops the money travelled" rather than "how far apart these are
        # in the drawing". The whole left-to-right reading depends on it.
        depths = layer_nodes(built)

        return {
            "seed": address,
            "chain": str(chain),
            "nodes": [
                {
                    "address": n.address,
                    "depth": depths.get(f"{chain}:{n.address}", depths.get(n.address, 0)),
                    "seed": n.is_seed,
                    "frontier": not n.expanded and not n.is_seed,
                    "label": n.label or from_sources.get(n.address, ("", ""))[0],
                    "category": n.category or from_sources.get(n.address, ("", ""))[1],
                }
                for n in built.nodes.values()
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "symbol": e.symbol,
                    "asset": e.asset or "",
                    "decimals": e.decimals,
                    "total_raw": str(e.total_raw),
                    "transfers": e.transfer_count,
                    # An edge is an aggregate over a span, so both ends travel.
                    # Without them every label read "[undated]", which is what
                    # the page said about data that has timestamps.
                    "first_seen": e.first_seen,
                    "last_seen": e.last_seen,
                }
                for e in built.edges.values()
            ],
            "assets": _assets_in(built, chain),
            "fetched": fetched,
            "fetch_complete": complete,
            "truncated": built.truncated,
            "frontier": sum(
                1 for n in built.nodes.values() if not n.expanded and not n.is_seed
            ),
        }

    def analyze(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Run one analyzer over the store and return its result verbatim.

        Warnings and hypotheses travel with the findings, not stripped for a
        tidier payload. A truncated walk and a complete one produce the same
        list of findings, and the warning is the only thing that distinguishes
        them; a hypothesis is capped at MEDIUM by construction and a UI that
        showed only findings would present every inference as an observation.
        """
        name = (_first(query, "name") or "").strip()
        address = (_first(query, "address") or "").strip()
        if not name or not address:
            raise ValueError("name and address are required")
        chain = self._chain(_first(query, "chain") or "1") or ChainId.evm(1)

        rows = self._transfers(address, chain)
        found = _run_over_store(name, rows, address, chain, _first(query, "subject") or address)
        return {
            "analyzer": name,
            "address": address,
            "findings": [
                {"title": f.title, "severity": f.severity.value, "detail": f.detail}
                for f in found.findings
            ],
            "hypotheses": [
                {
                    "claim": h.claim,
                    "confidence": h.confidence.name,
                    "score": round(h.score, 3),
                    "factors": [
                        {
                            "name": x.name,
                            "contribution": round(x.contribution, 3),
                            "note": x.note,
                        }
                        for x in h.factors
                    ],
                }
                for h in found.hypotheses
            ],
            "warnings": list(found.warnings),
        }

    def leads(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Open questions for the case, so the page can show what is unfinished."""
        from ..case.leads import LeadStore

        store = LeadStore(self.options.store.parent / "case.db")
        try:
            records = store.leads(_first(query, "address") or None, limit=100)
            counts = store.summary()
        finally:
            store.close()
        return {
            "counts": counts,
            "leads": [
                {
                    "id": r.id,
                    "address": r.address,
                    "kind": r.kind,
                    "value": r.value,
                    "verdict": r.verdict.value,
                    "verify_by": r.verify_by,
                    "reason": r.reason,
                }
                for r in records
            ],
        }

    def _transfers(self, address: str, chain: ChainId) -> list[Any]:
        from ..store.base import Query

        store = self._store()
        try:
            return list(store.transfers(Query(chain=chain, address=address, limit=5000)))
        finally:
            store.close()

    def note(self, body: dict[str, Any]) -> dict[str, Any]:
        """Record an observation against an address, in the case log.

        Not a free-floating sticky on a canvas. The commercial tools offer one
        and it is the wrong shape for this: a note that lives in a drawing is
        lost when the drawing is regenerated, carries no author and no time,
        and cannot be answered by "what is still open in this case".

        The case log already has all of that --- append-only, authored, with
        corrections that must name what they supersede --- so this writes there
        and the canvas reads it back. The picture is a view of the record
        rather than a place things are kept.
        """
        if not self.options.writable:
            raise PermissionError(
                "this server is read-only. Restart with --writable to record notes."
            )
        from ..case.log import CaseLog, Note, NoteKind

        body_text = str(body.get("body", "")).strip()
        if not body_text:
            raise ValueError("a note needs a body")
        kind = str(body.get("kind", "observation")).strip().lower()
        try:
            chosen = NoteKind(kind)
        except ValueError:
            raise ValueError(
                f"kind must be one of: {', '.join(k.value for k in NoteKind)}"
            ) from None
        if chosen is NoteKind.CORRECTION:
            # A correction must name what it replaces, and the page has no way
            # to choose that yet. Refusing is better than writing a correction
            # that supersedes nothing, which the log would reject anyway --- and
            # this says so in terms the caller can act on.
            raise ValueError(
                "a correction has to name the note it replaces, which this page "
                "cannot do yet. Use `chainscope note` for that"
            )

        who = self.options.analyst or ""
        log = CaseLog(self.options.store.parent / "case.db")
        try:
            note_id = log.add(
                Note(
                    at=datetime.now(timezone.utc),
                    # The configured analyst, never a value from the request ---
                    # the same reason `tag` will not take a caller-supplied
                    # source. A record that picks its own authorship is worse
                    # than one carrying none.
                    analyst=who or "browser",
                    identified_by="server" if who else "unattributed",
                    kind=chosen,
                    body=body_text,
                    subject=str(body.get("subject", "")).strip(),
                    chain=str(self._chain(str(body.get("chain", "")) or None) or "") or None,
                )
            )
        finally:
            log.close()
        return {"id": note_id, "analyst": who or "browser", "kind": chosen.value}

    def notes(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Notes filed against an address, so the canvas can show them."""
        from ..case.log import CaseLog

        log = CaseLog(self.options.store.parent / "case.db")
        try:
            found = log.notes(subject=_first(query, "subject") or "")
            superseded = log.superseded()
        finally:
            log.close()
        return {
            "notes": [
                {
                    "id": n.id,
                    "at": n.at.isoformat(),
                    "analyst": n.analyst,
                    "kind": n.kind.value,
                    "body": n.body,
                    "subject": n.subject,
                    # Kept and marked rather than dropped: "I thought X, then
                    # found Y" is the record, and a log showing only the final
                    # position is indistinguishable from one that was right
                    # first time.
                    "superseded": n.id in superseded,
                }
                for n in found[-200:]
            ]
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "store": str(self.options.store),
            "store_exists": self.options.store.exists(),
            "writable": self.options.writable,
            "categories": sorted(c.value for c in Category),
            "confidences": [c.name.lower() for c in Confidence],
        }


def _fetch_into(
    store: SqliteStore, address: str, chain: ChainId, *, max_pages: int = 15
) -> tuple[int, bool]:
    """Pull an address's transfers into the store, paging until they run out.

    Both directions, because inbound is where a poisoning transfer lives ---
    something done *to* the subject, which they never acknowledged.

    Returns ``(written, complete)``. The second is the whole point: a run that
    stopped on its page budget rather than on the data has read a prefix, and a
    prefix that does not announce itself is exactly the confidently-wrong answer
    this package is arranged against. The caller puts it on screen.

    An earlier attempt bisected the block range instead, because the truncation
    signal is an exception and carries no cursor. It starts above any chain head
    and spends its budget halving empty space: measured, 28 seconds to reach a
    single-block window and conclude, wrongly, that one block held a thousand
    transfers. Page numbers are what the upstream API actually offers.
    """
    provider = _asset_provider(chain)
    rows: list[Any] = []
    seen: set[tuple[str, int]] = set()
    complete = False

    for page in range(1, max_pages + 1):
        try:
            batch = provider.asset_transfers(
                chain, address, direction="all", limit=1000, page=page
            )
        except ResultTruncated as exc:
            # A full page is the expected outcome when paging, not a failure.
            # The signal still means "there is more", which is what the loop is
            # for; the rows it carries are this page.
            batch = exc.rows
        except ProviderError as exc:
            # An empty page is how a listing ends, and Blockscout reports it as
            # an error. Treating that as fatal would discard everything already
            # gathered --- and on a paged read, that is most of the answer.
            if "not found" in str(exc).lower() or "no token transfers" in str(exc).lower():
                complete = True
                break
            raise

        fresh = 0
        for transfer in batch:
            identity = (str(getattr(transfer.tx, "hash", "")), transfer.index)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(transfer)
            fresh += 1
        if len(batch) < 1000 or fresh == 0:
            # A short page is the end. Zero *new* rows on a full page means the
            # provider ignored `page` and served the first one again --- some
            # do --- and continuing would spin the budget for nothing.
            complete = True
            break

    if rows:
        store.put_transfers(rows)
    return len(rows), complete


def _assets_in(graph: Any, chain: ChainId) -> list[dict[str, Any]]:
    """Every asset on the graph, with what the impersonation check makes of it.

    Grouped rather than listed flat, because a filter that offers forty tickers
    in one column makes the reader do the classification --- and the tickers are
    chosen by the forger precisely to make that go wrong. Three of them read
    `ETH` and two read `USDC` on a real case, identical to the eye.

    Verdict travels per asset so the page can default to showing only what is
    genuine, and say what it hid rather than hiding it quietly.
    """
    from ..analysis.impersonation import inspect_assets

    seen: dict[str, dict[str, Any]] = {}
    for edge in graph.edges.values():
        key = (edge.asset or "").lower()
        row = seen.setdefault(
            key,
            {
                "asset": key,
                "symbol": edge.symbol,
                "decimals": edge.decimals,
                "edges": 0,
                "transfers": 0,
            },
        )
        row["edges"] += 1
        row["transfers"] += edge.transfer_count

    # `inspect_assets` wants transfer-shaped objects; an edge carries the two
    # fields it reads.
    probe = [
        SimpleNamespace(
            chain=chain,
            asset=SimpleNamespace(key=row["asset"]) if row["asset"] else None,
            amount=SimpleNamespace(symbol=row["symbol"], decimals=row["decimals"]),
        )
        for row in seen.values()
    ]
    verdicts = {a.contract: a for a in inspect_assets(probe, chain)}
    for key, row in seen.items():
        found = verdicts.get(key)
        row["verdict"] = found.verdict if found else "unlisted"
        row["resembles"] = found.resembles if found else ""
        row["why"] = " ".join(found.reasons) if found else ""
    return sorted(seen.values(), key=lambda r: (-r["transfers"], r["symbol"]))


def _asset_provider(chain: ChainId) -> Any:
    """The first provider that can enumerate transfers, asked directly.

    Not through `Router.enumerate`. Corroboration is the right default and the
    wrong tool here: it re-raises a truncation as "all providers failed", which
    is precisely the signal this function exists to page past.
    """
    from ..providers.build import router_for

    router, _skipped = router_for(chain)
    for candidate in router.candidates(chain, Capability.ASSET_TRANSFERS):
        return candidate
    raise ValueError(f"no provider can enumerate transfers on {chain}")


def _run_over_store(
    name: str, rows: list[Any], address: str, chain: ChainId, subject: str
) -> Any:
    """Dispatch to an analyzer that works over transfers already in the store.

    Only the store-based ones. The rest need a provider, which means spending
    somebody's rate limit --- a decision that belongs to `chainscope
    investigate`, not to a button. Asking for one of those says so instead of
    silently returning nothing, because an empty panel reads as "clean".
    """
    from ..analysis import contributors as contributors_mod
    from ..analysis import impersonation as impersonation_mod
    from ..analysis import poisoning as poisoning_mod
    from ..analysis import route as route_mod
    from ..core.result import Result

    if name == "impersonation":
        return impersonation_mod.analyse(rows, chain)
    if name == "poisoning":
        groups, examined = poisoning_mod.find_lookalikes(rows, address, chain=chain)
        return Result(
            analyzer=name,
            findings=tuple(poisoning_mod.findings(groups, examined)),
            hypotheses=tuple(poisoning_mod.hypotheses(groups, examined)),
        )
    if name == "contributors":
        inflow = contributors_mod.contributors(rows, address, subject, chain=chain)
        return Result(
            analyzer=name,
            findings=tuple(contributors_mod.findings(inflow)),
            warnings=(inflow.summary(),),
        )
    if name == "route":
        routes, notes = route_mod.find_routes(rows, subject, address, chain=chain)
        return Result(
            analyzer=name,
            findings=tuple(route_mod.findings(routes, notes, subject, address)),
        )
    raise ValueError(
        f"{name!r} is not one of the analyses this page can run. It offers the "
        f"ones that work over the store alone --- impersonation, poisoning, "
        f"contributors, route. The others need a provider, which spends a rate "
        f"limit, and that is `chainscope analyze {name}` rather than a button"
    )


def _make_handler(handlers: _Handlers) -> type[BaseHTTPRequestHandler]:
    options = handlers.options

    class Handler(BaseHTTPRequestHandler):
        server_version = "chainscope"
        sys_version = ""

        # -------------------------------------------------------------- utils

        def _origin_allowed(self) -> str | None:
            return origin_allowed(self.headers.get("Origin", ""), options.origins)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Never Allow-Credentials: there is no ambient session here, and
            # advertising one invites a page to try riding it.
            origin = self._origin_allowed()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            header = self.headers.get("Authorization", "")
            presented = header[7:] if header.lower().startswith("bearer ") else ""
            # Constant time: the token is short and an attacker on this machine
            # can make requests as fast as they like.
            return secrets.compare_digest(presented, options.token)

        def log_message(self, fmt: str, *args: Any) -> None:
            # Quiet by default. The default handler writes every request to
            # stderr, and a page probing this server would fill a terminal.
            return

        # ------------------------------------------------------------ methods

        def do_OPTIONS(self) -> None:
            # 200, not 204. `_send` always writes a JSON body and a
            # Content-Length, and a 204 carrying either is a protocol violation
            # -- some clients treat the body as the start of the next response
            # on a keep-alive connection.
            self._send(HTTPStatus.OK, {})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            # The page itself, unauthenticated and same-origin. It carries no
            # data --- every byte it displays comes from a request it makes back
            # here, and those are authorised. Requiring a token to fetch the
            # HTML would only put one in a URL somebody can copy.
            if parsed.path in ("/", "/index.html"):
                body = _page(options.token).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/health" and self._authorised():
                self._send(HTTPStatus.OK, handlers.health())
                return
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "bad or missing token"})
                return

            query = parse_qs(parsed.query)
            routes = {
                "/resolve": handlers.resolve,
                "/flows": handlers.flows,
                "/graph": handlers.graph,
                "/analyze": handlers.analyze,
                "/leads": handlers.leads,
                "/notes": handlers.notes,
            }
            route = routes.get(parsed.path)
            if route is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": f"no route {parsed.path}"})
                return
            self._guard(lambda: route(query))

        def do_POST(self) -> None:
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "bad or missing token"})
                return
            posted = urlparse(self.path).path
            if posted not in ("/tag", "/note"):
                self._send(HTTPStatus.NOT_FOUND, {"error": "no route"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": f"malformed JSON: {exc}"})
                return
            if not isinstance(body, dict):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object"})
                return
            route = handlers.tag if posted == "/tag" else handlers.note
            self._guard(lambda: route(body))

        def _guard(self, call: Any) -> None:
            """Run a route, mapping failures onto statuses a client can act on."""
            try:
                self._send(HTTPStatus.OK, call())
            except PermissionError as exc:
                self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"{type(exc).__name__}: {exc}"},
                )

    return Handler


class LocalServer:
    """A loopback HTTP API over one store."""

    def __init__(self, options: ServerOptions | None = None) -> None:
        self.options = options or ServerOptions()
        self.handlers = _Handlers(self.options)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.options.host}:{self.port}"

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self.options.port

    def start(self) -> LocalServer:
        """Bind and serve in a background thread. Returns self."""
        handler = _make_handler(self.handlers)
        self._httpd = ThreadingHTTPServer((self.options.host, self.options.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> LocalServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _analyst(stated: str) -> str:
    """Who to record on claims written through this server.

    Same rule as `chainscope tag`: an explicit value, then an identity somebody
    chose. An OS account yields empty --- a machine login is not authorship, and
    a claim signed with one is worse than a claim signed with nothing because it
    looks attributed.
    """
    from ..case.log import whoami

    if stated.strip():
        return stated.strip()
    identity = whoami()
    return identity.name if identity.is_chosen else ""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="chainscope-serve",
        description="Serve one store to the browser extension over loopback.",
    )
    parser.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind. The default keeps other machines out. Inside a "
        "container use 0.0.0.0 --- that binds the *container's* interfaces, and "
        "the boundary then lives in the published port (127.0.0.1:8787:8787)",
    )
    parser.add_argument(
        "--writable",
        action="store_true",
        help="allow the extension to record labels. Off by default.",
    )
    parser.add_argument(
        "--analyst",
        default="",
        help="who is tagging from the browser. Defaults to $CHAINSCOPE_ANALYST, "
        "then git's user.email. An OS account is not used --- a machine login "
        "is not authorship",
    )
    parser.add_argument(
        "--token",
        default="",
        help="reuse a token instead of generating one. Handy while developing; "
        "a fresh token per run is otherwise the safer default.",
    )
    args = parser.parse_args(argv)

    server = LocalServer(
        ServerOptions(
            store=args.store,
            host=args.host,
            port=args.port,
            writable=args.writable,
            token=args.token,
            analyst=_analyst(args.analyst),
        )
    ).start()

    print(f"chainscope serving {args.store} on {server.url}")
    print(f"  writable : {'yes' if args.writable else 'no (read-only)'}")
    print("\n  token, paste this into the extension's options page:\n")
    print(f"    {server.options.token}\n")
    if args.host in ("127.0.0.1", "localhost", "::1"):
        print(
            "  Loopback only. The token is what stops any open tab from reading "
            "your labels --\n  binding to 127.0.0.1 keeps other machines out and "
            "does nothing about this one."
        )
    else:
        # Said loudly because the usual reason to pass this is a container, and
        # the usual mistake is passing it on a laptop. The token is the only
        # control left once the interface is open.
        print(
            f"  ** Bound to {args.host}, not loopback. **\n"
            f"  Every machine that can route here can reach your label store, "
            f"and the token\n  is the only thing stopping them. This is correct "
            f"inside a container, where the\n  published port is the real "
            f"boundary. On a laptop it is almost certainly wrong."
        )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def _note(found: bool, unreachable: list[str]) -> str:
    """The sentence under the attribution panel.

    Three states, not two. "Nothing is recorded" and "a source could not be
    consulted" are different claims about the world, and collapsing them is the
    failure this module is most able to cause: a missing data file becomes a
    clean screening result phrased in the tool's own confident voice.
    """
    if unreachable:
        which = "; ".join(unreachable)
        head = (
            "Some sources could not be consulted, so this is incomplete"
            if found
            else "Nothing was found, but some sources could not be consulted --- "
            "this is not a clean result"
        )
        return f"{head}: {which}."
    if found:
        return ""
    return (
        "Nothing recorded for this address, and every configured source "
        "answered. That is not evidence it is unlabelled elsewhere, and "
        "certainly not that it is benign."
    )
