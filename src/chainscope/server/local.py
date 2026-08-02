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
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.attribution import Attribution, Category, Confidence, Method
from ..core.chainid import ChainId
from ..store.sqlite import SqliteStore

__all__ = ["LocalServer", "ServerOptions", "main"]

#: Body larger than this is refused unread. Nothing this API accepts is big,
#: and an unbounded read from a socket anyone on the machine can open is a
#: memory-exhaustion primitive.
MAX_BODY = 64 * 1024

#: Origins the extension may run from. Anything else gets no CORS headers, so
#: the browser refuses to hand the response to the page.
DEFAULT_ORIGINS = ("chrome-extension://", "moz-extension://")


@dataclass
class ServerOptions:
    store: Path = Path(".chainscope/store.db")
    host: str = "127.0.0.1"
    """Loopback. Binding wider would expose an unauthenticated-by-default
    label store to the network; the token is the second lock, not the first."""

    port: int = 8787
    token: str = ""
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

        source = str(body.get("source", "")).strip() or (f"browser:{self.options.agent_name}")
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

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "store": str(self.options.store),
            "store_exists": self.options.store.exists(),
            "writable": self.options.writable,
            "categories": sorted(c.value for c in Category),
            "confidences": [c.name.lower() for c in Confidence],
        }


def _make_handler(handlers: _Handlers) -> type[BaseHTTPRequestHandler]:
    options = handlers.options

    class Handler(BaseHTTPRequestHandler):
        server_version = "chainscope"
        sys_version = ""

        # -------------------------------------------------------------- utils

        def _origin_allowed(self) -> str | None:
            origin = self.headers.get("Origin", "")
            if not origin:
                return None
            for allowed in options.origins:
                if origin.startswith(allowed):
                    return origin
            return None

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
            self._send(HTTPStatus.NO_CONTENT, {})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health" and self._authorised():
                self._send(HTTPStatus.OK, handlers.health())
                return
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "bad or missing token"})
                return

            query = parse_qs(parsed.query)
            routes = {"/resolve": handlers.resolve, "/flows": handlers.flows}
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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="chainscope-serve",
        description="Serve one store to the browser extension over loopback.",
    )
    parser.add_argument("--store", type=Path, default=Path(".chainscope/store.db"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--writable",
        action="store_true",
        help="allow the extension to record labels. Off by default.",
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
            store=args.store, port=args.port, writable=args.writable, token=args.token
        )
    ).start()

    print(f"chainscope serving {args.store} on {server.url}")
    print(f"  writable : {'yes' if args.writable else 'no (read-only)'}")
    print("\n  token, paste this into the extension's options page:\n")
    print(f"    {server.options.token}\n")
    print(
        "  Loopback only. The token is what stops any open tab from reading "
        "your labels --\n  binding to 127.0.0.1 keeps other machines out and "
        "does nothing about this one."
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
