"""Assemble a populated router from installed providers and configuration.

Nothing did this. ``chainscope analyze`` constructed a bare ``Router()`` and
handed it to every analyzer, so each one asked "can any provider answer
ADDRESS_HISTORY here?", got no for the only possible reason --- there were no
providers at all --- and reported that the configured providers lacked the
capability. Meanwhile ``doctor`` read entry points directly, never built a
router, and listed the same capability as available.

Neither was lying. They were answering different questions and the difference
was invisible, which is the failure mode this project treats as the serious one.
:func:`router_for` is now the single answer to "what can actually run", and
``doctor`` uses it too so the two commands cannot drift apart again.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from ..config import Settings
from ..core.chainid import ChainId
from .base import Provider
from .router import Router

__all__ = ["build_providers", "provider_classes", "router_for"]


def provider_classes() -> tuple[dict[str, type[Provider]], dict[str, str]]:
    """Provider classes registered by this package or any plugin, and rejects.

    Same contract check as the analyzer registry: an entry point pointing at
    something that is not a :class:`Provider` is a defect in whatever registered
    it, and reporting it is cheaper than the alternative --- a user installing a
    plugin, seeing no change, and having nothing to read.
    """
    found: dict[str, type[Provider]] = {}
    broken: dict[str, str] = {}
    for ep in entry_points(group="chainscope.providers"):
        try:
            obj = ep.load()
        except Exception as exc:
            broken[ep.name] = f"failed to import ({type(exc).__name__}: {exc})"
            continue
        if not (isinstance(obj, type) and issubclass(obj, Provider)):
            broken[ep.name] = f"{ep.value} is not a Provider subclass"
            continue
        found[ep.name] = obj
    return found, broken


def build_providers(
    chain: ChainId, settings: Settings | None = None
) -> tuple[list[Provider], dict[str, str]]:
    """Every provider that can serve ``chain`` under ``settings``, and why not.

    The second element maps a provider name to the reason it was not built. It
    is the part worth having: "no provider offers ADDRESS_HISTORY" is a dead end,
    while "etherscan needs ETHERSCAN_API_KEY, and here is where to get one" is
    an instruction.
    """
    resolved = settings if settings is not None else Settings.load()
    classes, skipped = provider_classes()

    built: list[Provider] = []
    for name, cls in sorted(classes.items()):
        if not cls.serves(chain):
            # Not a problem to report. An Etherscan client cannot serve Bitcoin
            # however it is configured, and saying so for every chain would bury
            # the one line that matters.
            continue
        try:
            instances = cls.from_settings(resolved, chain)
        except Exception as exc:
            skipped[name] = f"could not be configured ({type(exc).__name__}: {exc})"
            continue
        if not instances:
            skipped[name] = _why_not(name, resolved)
            continue
        built.extend(instances)
    return built, skipped


def _why_not(name: str, settings: Settings) -> str:
    """The actionable half of "no provider available"."""
    from ..config import ENV_KEYS

    entry = ENV_KEYS.get(name)
    if entry:
        var, where = entry
        if not settings.credentials.get(name):
            return f"needs {var} -- {where}"
    return "not configured for this chain"


def router_for(
    chain: ChainId,
    settings: Settings | None = None,
    *,
    preferred: tuple[str, ...] = (),
) -> tuple[Router, dict[str, str]]:
    """A router carrying every provider that can serve ``chain``."""
    providers, skipped = build_providers(chain, settings)
    return Router(providers, preferred=preferred), skipped
