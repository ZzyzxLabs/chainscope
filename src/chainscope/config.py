"""Where credentials and endpoints come from.

Until now there was no answer to "how does this library get my API key", which
made the first ten minutes of using it an exercise in reading source. For a
project whose pitch is *clone it and start building*, that is the most expensive
possible place to have a gap.

Three rules shape this module.

**Environment first, file second.** ``os.environ`` overrides ``.env``, because
CI and container runtimes inject configuration that way and a checked-out file
must never win against it. The file exists for laptops.

**Credentials are :class:`~chainscope.transport.credentials.Secret`, not
:class:`str`.** They print as ``<redacted>``, so the ordinary leak paths --- a
traceback, a debug log, a settings object dumped into an issue --- close by
default rather than by discipline. Loading one also registers its value with the
scrubber, which is what keeps a live key out of a recorded cassette.

**Absence is not an error here.** A missing key is only a problem at the moment
something needs it, and failing at import time would make the offline test suite
unrunnable without credentials. :meth:`Settings.require` is where it becomes
loud, with the name of the variable and where to get one.

No dependency on ``python-dotenv``: the parser below is twenty lines, and a
library meant to be embedded in other people's tools should not spend a
dependency on that.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .transport.credentials import Secret

__all__ = ["ENV_KEYS", "ConfigError", "Settings", "load_dotenv"]


class ConfigError(RuntimeError):
    """Configuration is missing or unusable."""


#: Environment variable per provider, and where a reader gets one. The URL is
#: part of the contract: an error that says "set ETHERSCAN_API_KEY" and stops
#: has told the reader the one thing they already knew.
ENV_KEYS: dict[str, tuple[str, str]] = {
    "etherscan": (
        "ETHERSCAN_API_KEY",
        "https://etherscan.io/apis --- free, covers 60+ EVM chains",
    ),
    "alchemy": ("ALCHEMY_API_KEY", "https://alchemy.com --- free tier, archive RPC"),
    "helius": ("HELIUS_API_KEY", "https://helius.dev --- Solana RPC and history"),
    "trongrid": ("TRONGRID_API_KEY", "https://trongrid.io --- Tron"),
    "blockchair": ("BLOCKCHAIR_API_KEY", "https://blockchair.com/api --- Bitcoin, optional"),
}

#: RPC endpoints are read from ``CHAINSCOPE_RPC_<NAME>``, where ``<NAME>`` is the
#: chain's short name uppercased --- ``CHAINSCOPE_RPC_ETHEREUM``,
#: ``CHAINSCOPE_RPC_BSC``. Keyed by name rather than CAIP-2 because ``eip155:1``
#: is not a legal environment variable name.
RPC_PREFIX = "CHAINSCOPE_RPC_"


def load_dotenv(
    path: str | Path | None = None, *, search_from: Path | None = None
) -> dict[str, str]:
    """Parse a ``.env`` file into a plain dict. Never touches ``os.environ``.

    Returning rather than mutating is deliberate: a library that silently edits
    the process environment on import is a library that breaks the test suite of
    whatever embeds it.

    If ``path`` is omitted, walks up from ``search_from`` (default: the current
    directory) looking for ``.env``, so a CLI invoked from a subdirectory of a
    case folder still finds it.
    """
    if path is None:
        start = (search_from or Path.cwd()).resolve()
        for parent in (start, *start.parents):
            candidate = parent / ".env"
            if candidate.is_file():
                path = candidate
                break
        else:
            return {}

    file = Path(path)
    if not file.is_file():
        return {}

    out: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        # Strip one layer of matching quotes; an unbalanced quote is left alone
        # rather than half-stripped, which would silently corrupt the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration for one process."""

    credentials: dict[str, Secret] = field(default_factory=dict)
    """Keyed by provider name, as in :data:`ENV_KEYS`. Always populated for
    every known provider --- an unset one holds an empty :class:`Secret`, so
    callers never branch on presence in the dict."""

    rpc: dict[str, str] = field(default_factory=dict)
    """Chain short name (lowercased) to endpoint URL."""

    cache_dir: Path | None = None
    audit_log: Path | None = None
    rate_limit: float = 5.0
    timeout: float = 30.0

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv: str | Path | None = None,
        search_from: Path | None = None,
    ) -> Settings:
        """Read settings from the environment, falling back to a ``.env`` file."""
        environ: Mapping[str, str] = os.environ if env is None else env
        merged: dict[str, str] = {
            **load_dotenv(dotenv, search_from=search_from),
            **{k: v for k, v in environ.items() if v},
        }

        credentials = {
            provider: Secret(merged.get(var, "").strip(), provider)
            for provider, (var, _) in ENV_KEYS.items()
        }

        rpc = {
            name[len(RPC_PREFIX) :].lower(): value.strip()
            for name, value in merged.items()
            if name.startswith(RPC_PREFIX) and value.strip()
        }
        # An endpoint may itself embed a credential in its path. Registering the
        # whole URL means a cassette recorded against it cannot carry the key,
        # even where the path pattern is too short to match.
        for url in rpc.values():
            Secret(url, "rpc-endpoint")

        return cls(
            credentials=credentials,
            rpc=rpc,
            cache_dir=_path(merged.get("CHAINSCOPE_CACHE_DIR")),
            audit_log=_path(merged.get("CHAINSCOPE_AUDIT_LOG")),
            rate_limit=_number(
                merged.get("CHAINSCOPE_RATE_LIMIT"), 5.0, "CHAINSCOPE_RATE_LIMIT"
            ),
            timeout=_number(merged.get("CHAINSCOPE_TIMEOUT"), 30.0, "CHAINSCOPE_TIMEOUT"),
        )

    # ------------------------------------------------------------------ access

    def key(self, provider: str) -> Secret:
        """The credential for ``provider``, empty if unset."""
        if provider not in ENV_KEYS:
            raise ConfigError(
                f"unknown provider {provider!r}. Known: {', '.join(sorted(ENV_KEYS))}"
            )
        return self.credentials.get(provider, Secret("", provider))

    def require(self, provider: str) -> str:
        """The credential for ``provider``, or a message that says how to get one."""
        secret = self.key(provider)
        if not secret:
            var, where = ENV_KEYS[provider]
            raise ConfigError(
                f"{provider} needs a credential. Set {var} in your environment or "
                f"in a .env file.\n  Get one: {where}"
            )
        return secret.reveal()

    def has(self, provider: str) -> bool:
        return bool(self.key(provider))

    def configured(self) -> list[str]:
        """Providers with a credential present, for :command:`chainscope doctor`."""
        return sorted(name for name in ENV_KEYS if self.has(name))

    def rpc_for(self, chain: str) -> str | None:
        return self.rpc.get(chain.lower())

    def summary(self) -> dict[str, str]:
        """Human-readable state with no credential in it.

        Shows a four-character hint rather than nothing: "set" cannot
        distinguish the key you meant from the expired one you forgot about.
        """
        out = {name: self.key(name).hint() for name in sorted(ENV_KEYS)}
        for chain in sorted(self.rpc):
            out[f"rpc:{chain}"] = "configured"
        return out


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def _number(value: str | None, default: float, name: str) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        # Silently falling back to the default would hide a typo behind an
        # unexplained change in behaviour --- a rate limit that reverts to 5/s
        # looks like the remote being slow, not like a broken config line.
        raise ConfigError(f"{name}={value!r} is not a number") from exc
