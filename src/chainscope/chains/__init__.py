"""Chain adapters, and the one place that decides how an address compares.

:meth:`~chainscope.chains.base.ChainAdapter.normalize` is where the
case-sensitivity rule lives, and its docstring is blunt about the stakes: an
error there does not raise, it silently makes two addresses look identical or
one address look like two.

The rule was written once and then bypassed. The store called `.lower()`
directly on every address it touched --- so on Solana, Sui and Bitcoin it wrote
base58 into `transfers` and lowercase into `expanded`, and the two never
matched again. Measured: querying transfers by a Solana address returned zero
rows, and an address that had been expanded stayed on the frontier forever.

Hence :func:`adapter_for`. Reimplementing the rule anywhere else would drift ---
bech32 is lowercased, TRON folds hex into base58, Sui pads --- so nothing does.
`tests/unit/test_address_keys_agree.py` asserts the store and the adapters
cannot disagree.
"""

from __future__ import annotations

from functools import cache

from ..core.chainid import ChainId
from .base import ChainAdapter

__all__ = ["ChainAdapter", "adapter_for", "address_key"]

#: CAIP-2 namespace to adapter. Imported lazily so the store does not pay for
#: every chain package on first use.
_BY_NAMESPACE = {
    "eip155": ("chainscope.chains.evm", "EvmAdapter"),
    "bip122": ("chainscope.chains.bitcoin", "BitcoinAdapter"),
    "solana": ("chainscope.chains.solana", "SolanaAdapter"),
    "tron": ("chainscope.chains.tron", "TronAdapter"),
    "sui": ("chainscope.chains.sui", "SuiAdapter"),
}


@cache
def adapter_for(namespace: str) -> ChainAdapter | None:
    """The adapter for a CAIP-2 namespace, or ``None`` if none is built in.

    Cached: this sits on the hot path of every store write, and constructing an
    adapter per row would be the kind of quiet cost nobody attributes correctly.
    """
    target = _BY_NAMESPACE.get(namespace)
    if target is None:
        return None
    module_name, class_name = target
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:  # pragma: no cover - depends on install extras
        return None
    adapter: ChainAdapter = getattr(module, class_name)()
    return adapter


def address_key(chain: ChainId | str | None, raw: str) -> str:
    """The comparison form of ``raw`` on ``chain``.

    Falls back to the address **as written** when the chain is unknown or has no
    adapter --- never to `.lower()`. An unknown namespace is by definition not
    EVM, so lowercasing it could only destroy information, and the failure that
    causes (one address looking like two) is at least a miss rather than a false
    match between two people's addresses.
    """
    if chain is None:
        return raw.strip()
    namespace = str(chain).split(":", 1)[0]
    adapter = adapter_for(namespace)
    return adapter.normalize(raw) if adapter else raw.strip()
