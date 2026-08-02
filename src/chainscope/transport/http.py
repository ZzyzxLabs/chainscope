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
from dataclasses import dataclass, field
from typing import Any

import httpx

from .audit import AuditLog, redact_headers
from .cache import Cache, Volatility, cache_key
from .throttle import Throttle

__all__ = ["CircuitBreaker", "Client", "ReadOnlyViolation", "TransportError"]


class TransportError(RuntimeError):
    """A request failed after retries and fallbacks."""


class ReadOnlyViolation(PermissionError):
    """Something tried to mutate chain state.

    Deliberately a hard error rather than a warning. There is no legitimate use
    of this library that needs to sign or broadcast.
    """


#: Method prefixes that can move funds, sign, or reconfigure a node.
BLOCKED_PREFIXES: tuple[str, ...] = (
    "eth_send",
    "eth_sign",
    "eth_account",
    "personal_",
    "miner_",
    "admin_",
    "clique_",
    "les_",
    "parity_setr",
    "txpool_content",  # cheap to call, enormous to receive; not forensics
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
        cache: Cache | None = None,
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
    ) -> Any:
        key = cache_key("GET", url, params)
        if (hit := self._from_cache(key, volatility, url, provider)) is not _MISS:
            return hit
        return self._send("GET", url, key, volatility, provider, params=params, headers=headers)

    def post(
        self,
        url: str,
        payload: Any,
        *,
        volatility: Volatility = Volatility.SLOW,
        headers: dict[str, str] | None = None,
        provider: str | None = None,
    ) -> Any:
        key = cache_key("POST", url, payload)
        if (hit := self._from_cache(key, volatility, url, provider)) is not _MISS:
            return hit
        return self._send("POST", url, key, volatility, provider, json=payload, headers=headers)

    def rpc(
        self,
        url: str,
        method: str,
        params: list[Any] | None = None,
        *,
        volatility: Volatility = Volatility.SETTLED,
        headers: dict[str, str] | None = None,
        provider: str | None = None,
    ) -> Any:
        """JSON-RPC call. Rejects anything that could mutate state."""
        assert_read_only(method)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        key = cache_key("RPC", url_host(url), method, params)
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
        **kw: Any,
    ) -> Any:
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
                if self.cache is not None and volatility is not Volatility.NEVER:
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

        raise TransportError(f"{host}: failed after {self.max_retries} attempts: {last}")

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
