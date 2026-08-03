"""One place that decides which attribution sources are available.

There were two, and they disagreed. The web server discovered every source
whose data was present; the CLI required each to be named with a flag
(``--sanctions``, ``--nametags``, ``--local``) and knew nothing about the three
added later. So the same address resolved to a name in the browser and to
``unlabelled`` in the terminal, from one install, against one case directory.

An inconsistency like that is worse than a missing feature. A missing feature
is visible; this one tells two different stories about the same evidence and
gives the reader no reason to suspect either.

**Presence is the test, not configuration.** A source whose data file is absent
is not added, because a configured-but-empty source answers "nothing known" in
exactly the words a clean screening uses. :meth:`Source.ready` exists for that
distinction and this is where it is honoured.

**Order is by what a claim is worth.** The resolver merges without discarding,
but something has to be primary, and a published sanctions designation should
outrank a community list's guess. Ordering here rather than at each call site
is the point: three call sites ordering it themselves would eventually order it
three ways.
"""

from __future__ import annotations

from pathlib import Path

from .base import Source
from .resolver import Resolver

__all__ = ["DEFAULT_LABEL_DIR", "available_sources", "resolver_for"]

#: Where a case keeps its label data, relative to the case directory.
DEFAULT_LABEL_DIR = Path("data/labels")


def available_sources(base: Path | str = DEFAULT_LABEL_DIR) -> list[Source]:
    """Every built-in source whose data is actually present, strongest first.

    Imported inside the function so that a missing optional dependency in one
    adapter cannot stop the others being offered --- a source that fails to
    import is a packaging problem, and losing sanctions screening to it would
    be an absurd cost.
    """
    from .sources.contracts_list import ContractsListSource
    from .sources.darklist import DarklistSource
    from .sources.etherscan_dump import ExplorerDumpSource
    from .sources.ethlabels import EthLabelsSource
    from .sources.local import LocalSource
    from .sources.ofac import OfacSource

    root = Path(base)
    candidates: list[Source] = [
        # A government designation is a published legal fact and the only thing
        # here permitted to assert CERTAIN, so it answers first.
        OfacSource(root / "ofac.json"),
        # The user's own file next: a judgement they recorded outranks any list.
        LocalSource(root / "local.json"),
        ExplorerDumpSource(root / "nametags.json"),
        ContractsListSource(root / "contracts"),
        EthLabelsSource(root / "eth-labels"),
        DarklistSource(root / "darklist.json"),
    ]
    return [source for source in candidates if source.ready()]


def resolver_for(base: Path | str = DEFAULT_LABEL_DIR) -> Resolver:
    """A resolver over whatever is present. Empty is a legitimate answer.

    A caller that gets one with no sources should say so rather than report
    "nothing known": those read identically and mean opposite things.
    """
    return Resolver(available_sources(base))
