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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
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

        store = self._store()
        try:
            claims = [
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
            "note": (
                ""
                if claims
                else "Nothing recorded for this address in this store. That is "
                "not evidence it is unlabelled elsewhere, and certainly not "
                "that it is benign."
            ),
        }

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

        store = self._store()
        try:
            if not store.edges(address, chain, direction="out") and not store.edges(
                address, chain, direction="in"
            ):
                raise ValueError(
                    f"{address} is not in this store on {chain}. Nothing is drawn, "
                    f"because an empty graph and an unknown address look the same "
                    f"and mean opposite things. Run `chainscope investigate "
                    f"{address}` to fetch it"
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
                    "label": n.label or "",
                    "category": n.category or "",
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

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "store": str(self.options.store),
            "store_exists": self.options.store.exists(),
            "writable": self.options.writable,
            "categories": sorted(c.value for c in Category),
            "confidences": [c.name.lower() for c in Confidence],
        }


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
            if urlparse(self.path).path != "/tag":
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
            self._guard(lambda: handlers.tag(body))

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
