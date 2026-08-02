"""One definition of what counts as a credential, shared by everything that must not leak one.

Three layers need this answer and must not each invent their own: the audit log
(a log that leaks the key it recorded is worse than no log), the cache key (see
below), and the cassette recorder (a fixture committed to git with a live key in
it is a disclosed key). When those definitions drift, the weakest one decides
what actually leaks.

**The cache-key problem is the subtle one.** The obvious approach hashes the
whole request, credential included. Nothing leaks --- a SHA-256 is not
reversible --- so it looks safe, and it is. It is also useless, because the key
is now *personal*: the same query under a different API key produces a different
hash, so a cache handed to a colleague misses on every entry. That silently
breaks the promise in :mod:`chainscope.transport.cache` that a recorded cache
replays with no API keys and no network, which is the entire mechanism behind
case bundles.

So credentials are scrubbed *before* hashing. The resulting key describes the
question rather than the asker, which is the honest identity for a cached
answer: any valid key against the same endpoint gets the same chain data back.

The place this assumption could fail is a provider whose *plan* changes the
response --- a paid tier returning more rows for an identical query. No
explorer-family API this project supports behaves that way (limits are
per-request parameters, and those are still hashed), but a provider author who
adds one must not route it through this scrubber.
"""

from __future__ import annotations

import re
import threading
from typing import Any

__all__ = [
    "SECRET_HEADERS",
    "SECRET_PARAMS",
    "Secret",
    "forget_secret",
    "redact",
    "redact_headers",
    "register_secret",
    "scrub_params",
    "scrub_value",
]

#: Placeholder substituted for a credential. Constant, so that scrubbing is
#: deterministic and two callers with different keys agree on a cache key.
PLACEHOLDER = "<redacted>"

#: Query-string parameters whose values are credentials. Matched
#: case-insensitively against the *whole* parameter name.
SECRET_PARAMS: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "api-key",
        "key",
        "token",
        "access_token",
        "auth",
        "auth_token",
        "secret",
        "password",
        "passwd",
        "signature",
        "sig",
        "session",
    }
)

SECRET_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-apikey",
        "tron-pro-api-key",
        "x-auth-token",
        "x-access-token",
        "cookie",
        "set-cookie",
    }
)

_PARAM_RE = re.compile(r"(?i)\b(" + "|".join(sorted(SECRET_PARAMS)) + r")=([^&\s]+)")

# Keys embedded in a path segment rather than a query string --- the Alchemy and
# Helius convention, e.g. ``/v2/<key>``. Only long opaque segments match, so
# ordinary paths such as ``/v2/api`` survive.
_PATH_KEY_RE = re.compile(r"/(v\d+)/([A-Za-z0-9_-]{20,})")


def is_secret_param(name: str) -> bool:
    return name.strip().lower() in SECRET_PARAMS


# --------------------------------------------------------------------- registry

# Literal credential values seen at runtime. The pattern-based rules above catch
# credentials in the shapes we anticipated; this catches the ones we did not ---
# a key interpolated into a path segment too short for _PATH_KEY_RE, or echoed
# back inside a response body. Recording a cassette is exactly when that second
# case matters, because the result gets committed to a public repository.
#
# Values only, never names, and never persisted.
_KNOWN: set[str] = set()

# Providers register credentials from whichever thread constructs them, while a
# sweep is already scrubbing responses on others. Iterating a set during a write
# raises, and it would raise inside the code path whose job is to stop a key
# reaching a log.
_KNOWN_LOCK = threading.Lock()

#: Below this length, a "credential" is more likely to be a common substring
#: whose blind replacement would corrupt unrelated data.
_MIN_REGISTERED_LENGTH = 12


def register_secret(value: str | None) -> None:
    """Remember a literal credential so it can be scrubbed wherever it appears."""
    if value and len(value) >= _MIN_REGISTERED_LENGTH:
        with _KNOWN_LOCK:
            _KNOWN.add(value)


def forget_secret(value: str | None) -> None:
    with _KNOWN_LOCK:
        _KNOWN.discard(value or "")


def scrub_value(text: str) -> str:
    """Replace any registered credential appearing anywhere in ``text``.

    Longest first: an endpoint URL that contains a key is itself registered, and
    replacing the shorter key inside it first would leave a mangled remainder
    that no longer matches the longer entry.
    """
    # Snapshot under the lock, then replace outside it: the replacements are
    # the expensive part and nothing about them needs the set held.
    with _KNOWN_LOCK:
        known = sorted(_KNOWN, key=len, reverse=True)
    for secret in known:
        if secret in text:
            text = text.replace(secret, PLACEHOLDER)
    return text


# --------------------------------------------------------------------- scrubbing


def redact(text: str) -> str:
    """Strip credentials from a URL or arbitrary string."""
    out = _PARAM_RE.sub(r"\1=" + PLACEHOLDER, text)
    out = _PATH_KEY_RE.sub(r"/\1/" + PLACEHOLDER, out)
    return scrub_value(out)


def endpoint_identity(url: str) -> str:
    """A cache-safe identity for an endpoint: no credential, but still an endpoint.

    :func:`redact` is for logs, where erasing too much is free. Using it to build
    a cache key is not: an endpoint registered whole as a credential --- which is
    what happens when an RPC URL embeds its key --- reduces to ``<redacted>``,
    and every chain configured that way collapses onto one cache entry. A query
    for Ethereum then returns whatever BSC answered.

    That was not hypothetical. ``rpc.ankr.com/eth/<key>`` and
    ``rpc.ankr.com/bsc/<key>`` both became ``<redacted>``; so did two public
    nodes carrying no credential at all, because the whole URL had been
    registered regardless.

    So this strips only the parts that can *be* a credential --- **userinfo**,
    long opaque path segments, and the query string --- and keeps scheme, host,
    port, and the path structure that says which chain this is.

    Userinfo was not stripped, and it is the most direct credential a URL can
    carry: `https://user:s3cr3t@rpc.example.com/eth` produced an identity
    containing `s3cr3t`. Cache keys are written into the cache database, and a
    cache database is what a `Bundle` ships to a third party --- so the leak had
    a distribution path built for it.

    `scrub_value` runs over each path **segment**, not over the finished
    string. Over the whole identity it reintroduces the failure this function
    exists to prevent: an endpoint registered whole as a credential --- which is
    what the very next paragraph of this docstring describes --- reduces to
    `<redacted>` and every chain configured that way collapses onto one cache
    entry again. `test_two_chains_never_share_a_cache_entry` catches it, and
    caught it when this was tried.

    Per segment is the useful half: a registered key sitting in a path is
    removed, and the host and path structure that distinguish two endpoints
    survive.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.netloc:
        # Not a URL. Fall back to full redaction; an unrecognised string is
        # more likely to be a bare credential than an endpoint.
        return redact(url)

    segments = [
        PLACEHOLDER if _looks_opaque(segment) else scrub_value(segment)
        for segment in parts.path.split("/")
    ]
    # hostname/port rather than netloc: netloc carries `user:password@`, and
    # keeping it made a "cache-safe identity" the least safe string in the file.
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # The query is dropped rather than scrubbed: for the callers that build one,
    # the parameters travel separately and are hashed there.
    return f"{parts.scheme}://{host}{'/'.join(segments)}"


#: Path segments this long and this uniform are keys, not routes. Chain names
#: ("eth", "bsc", "mainnet") and API versions are far shorter.
_OPAQUE_SEGMENT_LENGTH = 20


def _looks_opaque(segment: str) -> bool:
    if len(segment) < _OPAQUE_SEGMENT_LENGTH:
        return False
    return all(c.isalnum() or c in "-_" for c in segment)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: (PLACEHOLDER if k.lower() in SECRET_HEADERS else redact(v))
        for k, v in headers.items()
    }


def scrub_params(params: Any) -> Any:
    """Return ``params`` with credential values replaced, structure preserved.

    Recurses, because a credential can sit inside a nested JSON-RPC payload as
    easily as in a flat query string. Non-container values pass through
    untouched: this runs on the hot path of every request, and rewriting every
    string in every body would be both slow and lossy.
    """
    if isinstance(params, dict):
        return {
            k: (PLACEHOLDER if isinstance(k, str) and is_secret_param(k) else scrub_params(v))
            for k, v in params.items()
        }
    if isinstance(params, (list, tuple)):
        return [scrub_params(v) for v in params]
    if isinstance(params, str):
        return scrub_value(params)
    return params


# --------------------------------------------------------------------- Secret


class Secret:
    """A string that does not print itself.

    Credentials leak through the boring paths --- a repr in a traceback, a
    dataclass echoed into a debug log, an exception message pasted into an
    issue. Making the value inaccessible except through an explicit
    :meth:`reveal` turns each of those from an accident into a deliberate act.

    Constructing one registers the value for scrubbing, so a key that reaches a
    URL or a response body is caught even where the pattern rules would miss it.
    """

    __slots__ = ("_value", "label")

    def __init__(self, value: str, label: str = "secret") -> None:
        self._value = value
        self.label = label
        register_secret(value)

    def reveal(self) -> str:
        """Return the underlying value. Deliberately verbose at the call site."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        if not self._value:
            return f"Secret({self.label!r}, unset)"
        return f"Secret({self.label!r}, {PLACEHOLDER}, len={len(self._value)})"

    __str__ = __repr__

    def hint(self) -> str:
        """Last four characters, for telling two configured keys apart."""
        if not self._value:
            return "unset"
        return f"...{self._value[-4:]}" if len(self._value) > 8 else "set"
