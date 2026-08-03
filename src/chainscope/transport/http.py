"""HTTP layer: cache, throttle, retry, circuit breaker, audit --- and no write path.

The read-only guarantee lives here rather than in a policy document. JSON-RPC
method names are checked against a deny list before a request is built, so a
provider cannot broadcast a transaction even if its author wanted to. A forensics
tool that can move funds is a liability to whoever runs it.

The circuit breaker exists because free public endpoints fail constantly and
independently. Without one, every query pays the timeout for the same dead host
over and over, and a fifty-address sweep takes twenty minutes instead of thirty
seconds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .audit import AuditLog
from .cache import CacheBackend, Volatility, cache_key
from .credentials import endpoint_identity, redact_headers, scrub_params
from .throttle import Throttle

__all__ = ["Cacheable", "CircuitBreaker", "Client", "ReadOnlyViolation", "TransportError"]

#: Predicate deciding whether a decoded response body is worth keeping.
#:
#: It exists because HTTP status is not a reliable signal of success. Etherscan
#: answers a rate limit with ``200 OK`` and ``{"status": "0", "result": "Max
#: calls per sec rate limit reached"}``; several RPC providers do the same. The
#: transport sees a perfectly good response and caches it, and from then on the
#: address returns "rate limited" from cache, with no request made, for as long
#: as the TTL lasts --- an hour for ``SLOW``, and forever in a cassette.
#:
#: That is the project's central failure mode ("we do not know" stored as if it
#: were data) occurring one layer *below* the provider code that handles it so
#: carefully. The transport cannot recognise sixty different error shapes, and
#: the provider cannot reach inside the caching. So the provider supplies the
#: predicate and the transport applies it.
Cacheable = Callable[[Any], bool]


class TransportError(RuntimeError):
    """A request failed after retries and fallbacks."""


class ReadOnlyViolation(PermissionError):
    """Something tried to mutate chain state.

    Deliberately a hard error rather than a warning. There is no legitimate use
    of this library that needs to sign or broadcast.
    """


#: Method prefixes that can move funds, sign, or reconfigure a node.
#:
#: A **deny-list**, deliberately, and the alternative is worth naming: an
#: allow-list of known-safe methods would block every read this project has not
#: enumerated. Solana, Sui and Tron read methods are bare names --- `getBalance`,
#: `sui_getObject`, `wallet/getaccount` --- and a plugin provider serving a chain
#: nobody here has heard of would be refused at every call. A deny-list fails
#: open on an unknown *read*, which is recoverable; an allow-list fails closed on
#: every unknown read, which makes the library unusable by the people it is meant
#: to be extended by.
#:
#: That trade only holds if the list actually covers what mutates. It did not.
#: Measured across the five supported ecosystems, it blocked **one** of ten
#: mutating methods: Solana's `sendTransaction`, Sui's
#: `sui_executeTransactionBlock`, Tron's broadcast endpoint, the whole engine
#: API, `debug_setHead` and every `parity_set*` all passed. The list was
#: EVM-shaped, and only partly that --- `parity_setr` is a typo for `parity_set`
#: and matched no method that exists.
BLOCKED_PREFIXES: tuple[str, ...] = (
    # --- EVM ---------------------------------------------------------------
    "eth_send",
    "eth_sign",
    "eth_account",
    "eth_submit",  # submitWork, submitHashrate
    "personal_",
    "miner_",
    "admin_",
    "clique_",
    "les_",
    "parity_set",  # was `parity_setr`, which matched nothing
    "debug_set",  # setHead rewinds the node
    "engine_",  # consensus API: forkchoiceUpdated, newPayload
    "db_put",
    "shh_post",
    "txpool_content",  # cheap to call, enormous to receive; not forensics
    # --- Solana ------------------------------------------------------------
    # Bare names, so these are the whole method rather than a family.
    "sendtransaction",
    "requestairdrop",
    "simulatetransaction",  # harmless on-node, but only ever precedes a send
    # --- Sui ---------------------------------------------------------------
    # `sui_dryRun…` and `sui_devInspect…` are reads and stay allowed.
    "sui_execute",
    "sui_signandexecute",
    "unsafe_",  # unsafe_transferObject, unsafe_moveCall, … all build txs
    # --- Tron --------------------------------------------------------------
    "wallet/broadcast",
    "wallet/createtransaction",
    "wallet/easytransfer",
    "wallet/sign",
)


def assert_read_only(method: str) -> None:
    """Raise if ``method`` could change anything."""
    lowered = method.lower()
    if lowered.startswith(BLOCKED_PREFIXES):
        raise ReadOnlyViolation(
            f"{method!r} is blocked: chainscope is read-only by construction. "
            f"If you are trying to broadcast a transaction, you want a wallet, "
            f"not a forensics tool."
        )


#: Request fields that name an operation. ``method`` is JSON-RPC; ``action`` is
#: the explorer convention, and Etherscan-family APIs really do expose
#: ``?module=proxy&action=eth_sendRawTransaction``.
_METHOD_FIELDS: tuple[str, ...] = ("method", "action")


def assert_payload_read_only(payload: Any) -> None:
    """Raise if any operation named anywhere in a request body could mutate state.

    :func:`assert_read_only` guards one method name, which covers
    :meth:`Client.rpc` and nothing else. Two paths go around it: a hand-built
    :meth:`Client.post`, and a JSON-RPC *batch*, where the method names live
    inside a list and there is no single ``method`` argument to check.

    Both are ordinary things for a provider author to write, so the guarantee
    has to be enforced where every request converges rather than at the one
    convenient entry point. "Read-only by construction" is only true if it holds
    for code written by someone who never read this docstring.

    **Anywhere means anywhere**, and it did not. This descended into a top-level
    list and then checked one level of dict keys, so
    ``{"requests": [{"method": "eth_sendRawTransaction"}]}`` --- an ordinary
    shape for a batching gateway --- went straight through. The docstring above
    has always said *anywhere*; the code checked two shapes. A safety property
    written down and not enforced is worse than one nobody claimed, because the
    claim is what people rely on.

    So: **every string, at every depth**, whether it sits in a value, a key, or
    a bare list element. Not only the fields named `method`.

    That is deliberately blunt. A guard that only understands the shapes it was
    shown protects the shapes it was shown, and a provider author reaching for
    a batching gateway or an RPC keyed by method name gets no protection at the
    moment they most need it. The cost is that a request carrying the literal
    string ``eth_sendRawTransaction`` as *data* is refused --- and a request with
    that in it is far more likely to be an attempt than a coincidence. For a
    safety property, refusing the ambiguous case is the correct direction, and
    the error names the string so it can be diagnosed in one look.
    """
    if isinstance(payload, str):
        assert_read_only(payload)
        return
    if isinstance(payload, (list, tuple, set, frozenset)):
        for item in payload:
            assert_payload_read_only(item)
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_payload_read_only(key)
            assert_payload_read_only(value)


@dataclass
class CircuitBreaker:
    """Skip a host that keeps failing, then let it prove itself again.

    Half-open recovery matters: a permanently open breaker turns a transient
    outage into a permanent loss of a data source, which is how a provider that
    briefly rate-limited you ends up never being used again.
    """

    threshold: int = 5
    cooldown: float = 60.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_open(self, host: str) -> bool:
        with self._lock:
            opened = self._opened_at.get(host)
            if opened is None:
                return False
            if time.monotonic() - opened >= self.cooldown:
                # Half-open: allow one probe through.
                del self._opened_at[host]
                self._failures[host] = self.threshold - 1
                return False
            return True

    def record_success(self, host: str) -> None:
        with self._lock:
            self._failures.pop(host, None)
            self._opened_at.pop(host, None)

    def record_failure(self, host: str) -> None:
        with self._lock:
            n = self._failures.get(host, 0) + 1
            self._failures[host] = n
            if n >= self.threshold:
                self._opened_at[host] = time.monotonic()

    def status(self) -> dict[str, str]:
        with self._lock:
            return {
                h: ("open" if h in self._opened_at else f"{n} failures")
                for h, n in self._failures.items()
            }


class Client:
    """The single outbound path. Providers do not call ``httpx`` directly."""

    def __init__(
        self,
        *,
        cache: CacheBackend | None = None,
        throttle: Throttle | None = None,
        audit: AuditLog | None = None,
        breaker: CircuitBreaker | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = "chainscope/0.1 (+https://github.com/ZzyzxLabs/chainscope)",
    ) -> None:
        self.cache = cache
        self.throttle = throttle or Throttle()
        self.audit = audit or AuditLog(None, enabled=False)
        self.breaker = breaker or CircuitBreaker()
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        return self._client

    # ---------------------------------------------------------------- requests

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        volatility: Volatility = Volatility.SLOW,
        headers: dict[str, str] | None = None,
        provider: str | None = None,
        cacheable: Cacheable | None = None,
    ) -> Any:
        key = _request_key("GET", url, params)
        if (hit := self._from_cache(key, volatility, url, provider)) is not _MISS:
            return hit
        return self._send(
            "GET",
            url,
            key,
            volatility,
            provider,
            cacheable=cacheable,
            params=params,
            headers=headers,
        )

    def post(
        self,
        url: str,
        payload: Any,
        *,
        volatility: Volatility = Volatility.SLOW,
        headers: dict[str, str] | None = None,
        provider: str | None = None,
        cacheable: Cacheable | None = None,
    ) -> Any:
        key = _request_key("POST", url, payload)
        if (hit := self._from_cache(key, volatility, url, provider)) is not _MISS:
            return hit
        return self._send(
            "POST",
            url,
            key,
            volatility,
            provider,
            cacheable=cacheable,
            json=payload,
            headers=headers,
        )

    def rpc(
        self,
        url: str,
        method: str,
        params: list[Any] | None = None,
        *,
        volatility: Volatility = Volatility.SETTLED,
        headers: dict[str, str] | None = None,
        provider: str | None = None,
        scope: str | None = None,
    ) -> Any:
        """JSON-RPC call. Rejects anything that could mutate state.

        ``scope`` names what the answer is *about* --- in practice the
        :class:`~chainscope.core.chainid.ChainId`. It is the cache key, and
        getting it right matters in both directions.

        Keying on the endpoint host is wrong: ``rpc.example.com/eth`` and
        ``rpc.example.com/bsc`` share a host, so the same ``eth_getCode`` on the
        same address would collide and the second chain would silently receive
        the first chain's answer. Multi-chain work — the same attacker contract
        deployed at one address on four networks — walks straight into it.

        Keying on the full URL fixes the collision and introduces a smaller
        problem: the cache stops being portable. A bundle recorded against one
        node cannot replay against another, and rotating a key embedded in a URL
        throws the cache away.

        Chain identity is the honest key, because it is what actually decides
        the answer: any correct BSC node returns the same code for the same
        address at the same block. Callers that cannot name a scope fall back to
        the full URL, which is merely unportable rather than wrong.
        """
        assert_read_only(method)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        # `scope` is already credential-free (it names a chain). The URL
        # fallback is not: Alchemy and Helius put the key in the path, so an
        # unscrubbed URL would make every cached entry personal to one account.
        key = cache_key("RPC", scope or endpoint_identity(url), method, scrub_params(params))
        if (hit := self._from_cache(key, volatility, url, provider)) is not _MISS:
            return hit

        body = self._send(
            url=url,
            method_="POST",
            key=key,
            volatility=Volatility.NEVER,
            provider=provider,
            json=payload,
            headers=headers,
        )
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            raise TransportError(f"{method}: {err.get('message', err)}")
        result = body.get("result") if isinstance(body, dict) else body
        if self.cache is not None:
            self.cache.put(key, result, volatility, provider=provider)
        return result

    # ---------------------------------------------------------------- internals

    def _from_cache(
        self, key: str, volatility: Volatility, url: str, provider: str | None
    ) -> Any:
        if self.cache is None:
            return _MISS
        hit = self.cache.get(key, volatility)
        if hit is None:
            return _MISS
        self.audit.log("cache.hit", url, provider=provider, cache_key=key, cached=True)
        return hit

    def _send(
        self,
        method_: str,
        url: str,
        key: str,
        volatility: Volatility,
        provider: str | None,
        cacheable: Cacheable | None = None,
        **kw: Any,
    ) -> Any:
        # The single choke point every outbound request passes through, and
        # therefore the only place the read-only guarantee can be complete.
        # Checking in rpc() alone leaves post() and JSON-RPC batches open.
        for candidate in (kw.get("json"), kw.get("params")):
            if candidate is not None:
                assert_payload_read_only(candidate)

        host = url_host(url)
        if self.breaker.is_open(host):
            raise TransportError(f"{host}: circuit open (too many recent failures)")

        last: Exception | None = None
        for attempt in range(self.max_retries):
            self.throttle.acquire(url)
            started = time.monotonic()
            try:
                resp = self._http().request(method_, url, **_clean(kw))
                elapsed = (time.monotonic() - started) * 1000
                self.audit.log(
                    f"http.{method_.lower()}",
                    url,
                    provider=provider,
                    status=resp.status_code,
                    cache_key=key,
                    duration_ms=elapsed,
                    params=_safe_params(kw),
                )
                if resp.status_code == 429:
                    # The operator is telling us the rate directly; believe them.
                    self.throttle.set_rate(host, max(1.0, self.throttle.default_rate / 2))
                    last = TransportError(f"{host}: rate limited")
                    time.sleep(2.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = _decode(resp)
                self.breaker.record_success(host)
                # `cacheable` guards against storing an error that arrived
                # dressed as a success -- see the Cacheable docstring. A body
                # the provider rejects is still returned; it is simply not
                # remembered, so the next attempt is a real one.
                # A predicate that raises must not lose a response that
                # arrived fine. The worst a broken one can do is cost a cache
                # entry; turning a 200 into an exception would cost the answer.
                keep = volatility is not Volatility.NEVER
                if keep and cacheable is not None:
                    try:
                        keep = bool(cacheable(data))
                    except Exception:
                        keep = False
                if self.cache is not None and keep:
                    self.cache.put(key, data, volatility, provider=provider)
                return data
            except httpx.HTTPError as exc:
                last = exc
                self.audit.log(
                    f"http.{method_.lower()}",
                    url,
                    provider=provider,
                    cache_key=key,
                    error=type(exc).__name__,
                )
                self.breaker.record_failure(host)
                time.sleep(0.5 * (attempt + 1))

        # The body, when there was one.
        #
        # httpx's message for a 4xx is "Client error '400 Bad Request' for url
        # ...", which says a request was refused and nothing about why. The
        # reason is almost always in the body --- "query returned more than
        # 10000 results", "invalid params: trailing null in topics" --- and
        # discarding it turns a five-second fix into an afternoon of guessing.
        # Truncated, because an HTML error page is not worth a screen of noise.
        detail = ""
        if isinstance(last, httpx.HTTPStatusError):
            try:
                body = last.response.text.strip()
            except Exception:
                body = ""
            if body:
                detail = f"\n  the server said: {body[:400]}"
        raise TransportError(
            f"{host}: failed after {self.max_retries} attempts: {last}{detail}"
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Missing:
    __slots__ = ()


_MISS = _Missing()


def url_host(url: str) -> str:
    return Throttle.host_of(url)


def _request_key(verb: str, url: str, payload: Any) -> str:
    """Cache key for a request, with credentials removed before hashing.

    Hashing the credential too would leak nothing --- SHA-256 is not
    reversible --- but it makes the key personal: the same query under a
    different API key lands in a different slot. A cache handed to a colleague
    would then miss on every entry, which quietly falsifies the reproducibility
    guarantee in :mod:`chainscope.transport.cache` and empties case bundles of
    their point.

    What remains identifies the *question*, which is the honest key for a cached
    answer: any valid credential against the same endpoint returns the same
    chain data.

    The endpoint goes through :func:`endpoint_identity` rather than
    :func:`redact`, and the difference is not cosmetic. Full redaction erased
    the host along with the key, so two chains served from one provider shared
    a cache entry and an Ethereum query could return BSC's answer.
    """
    return cache_key(verb, endpoint_identity(url), scrub_params(payload))


def _clean(kw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v is not None}


def _safe_params(kw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(kw.get("params"), dict):
        out.update(kw["params"])
    if isinstance(kw.get("headers"), dict):
        out["_headers"] = redact_headers(kw["headers"])
    if isinstance(kw.get("json"), dict) and "method" in kw["json"]:
        out["method"] = kw["json"]["method"]
    return out


def _decode(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
