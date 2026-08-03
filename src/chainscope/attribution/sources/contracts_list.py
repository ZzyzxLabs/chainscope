"""Named contracts, from the community registry that records its own sources.

`ethereum-lists/contracts` is the largest open contract registry that is
maintained rather than merely published: 60,000-odd entries across many chains,
maintainer-reviewed pull requests, and --- the part that matters here --- a
``source`` field on every record saying where the name came from.

That field is why this sits a step above :mod:`.ethlabels` in usefulness and
the same step below in confidence. An entry looks like::

    {"project": "topbidder", "name": "BID", "source": "dune"}

so the claim is traceable to a body that made it. It is still somebody else's
judgement, recorded without evidence, which is why this asserts ``MEDIUM``.

**The repository declares no licence, and that is not a detail.** GitHub
reports none, so the default applies: no permission to redistribute. This
package therefore fetches rather than bundles, marks the source
``redistributable=False``, and the fetch is a shallow `git clone` a user runs
knowingly. Shipping the data would be a licence violation dressed as
convenience.

**One file per contract, so it is cloned rather than crawled.** 60,000 HTTP
requests to build a label table is not a thing to do to somebody's rate limit,
or to GitHub. A depth-1 clone is 45 MB and one operation.

**A contract name is not an entity attribution.** "This contract belongs to the
topbidder project" is a much narrower claim than "this address is controlled by
X", and the category reflects it: :attr:`Category.CONTRACT` unless the record
names something better. Reading a deployment as an ownership claim is the error
this distinction exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ...core.attribution import Attribution, Category, Confidence, Method
from ...core.chainid import ChainId
from ..base import Source, SourceError, SourceMeta

__all__ = ["REPOSITORY", "ContractsListSource", "clone"]

REPOSITORY = "https://github.com/ethereum-lists/contracts"


class ContractsListSource(Source):
    """Contract names from a local clone of ethereum-lists/contracts.

    Expected layout, exactly as the repository has it::

        <path>/contracts/<chain reference>/<0xAddress>.json

    Read from disk per lookup rather than indexed at startup. 60,000 files is
    slow to walk and most of a case never touches them, so the cost is paid per
    address that is actually asked about --- and a missing file is the answer
    "not in the registry", which is the common one.
    """

    name = "contracts_list"

    def __init__(self, path: Path | str = "data/labels/contracts") -> None:
        self.path = Path(path)
        self.meta = SourceMeta(
            publisher="ethereum-lists/contracts contributors",
            # Stated as it is, not guessed. GitHub reports no licence, so the
            # default applies and nothing here may be redistributed.
            license="none declared --- fetched, never bundled",
            redistributable=False,
            url=REPOSITORY,
        )
        self._index: dict[str, dict[str, Path]] = {}

    def ready(self) -> bool:
        return (self.path / "contracts").is_dir()

    def _index_for(self, reference: str, directory: Path) -> dict[str, Path]:
        """Filename index for one chain, built once.

        Globbing per lookup cost 268ms against Ethereum's hundred thousand
        files --- five seconds for a twenty-node graph, which is the difference
        between a source somebody leaves switched on and one they turn off.

        Keyed lower-case, because filenames are checksummed upstream and callers
        arrive with any spelling. Per chain, so a case touching one chain never
        pays for the others.
        """
        cached = self._index.get(reference)
        if cached is None:
            cached = {p.stem.lower(): p for p in directory.glob("0x*.json")}
            self._index[reference] = cached
        return cached

    def lookup(self, address: str, chain: ChainId | None = None) -> list[Attribution]:
        """What the registry calls this contract, on this chain.

        A chain is required. The registry is keyed by chain reference and the
        same twenty bytes are a different contract on each --- answering for
        "whichever chain has an entry" would attribute a name on Ethereum to a
        deployment on BSC, which is exactly the confusion the key exists to
        avoid.
        """
        if not self.ready():
            raise SourceError(
                f"no clone under {self.path}. Fetch it with "
                f"`chainscope.attribution.sources.contracts_list.clone()`, which "
                f"is a depth-1 git clone of {REPOSITORY} (~45 MB). Until then "
                f"this source reports nothing, and nothing is not the same as "
                f"unknown"
            )
        if chain is None:
            raise SourceError(
                "this registry is keyed by chain: the same address is a different "
                "contract on each, so a chainless lookup would attribute one "
                "deployment's name to another"
            )

        directory = self.path / "contracts" / chain.reference
        if not directory.is_dir():
            # A chain the registry does not cover. Distinct from "this contract
            # is not listed", and the caller is told which.
            raise SourceError(
                f"the registry has no entries for {chain}; that is a gap in it "
                f"rather than a statement about this address"
            )

        found = self._index_for(chain.reference, directory).get(address.strip().lower())
        if found is None:
            return []
        try:
            record = json.loads(found.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"{found} could not be read: {exc}") from exc
        if not isinstance(record, dict):
            return []

        project = str(record.get("project") or "").strip()
        contract_name = str(record.get("name") or "").strip()
        upstream = str(record.get("source") or "").strip()
        label = " ".join(part for part in (project, contract_name) if part)
        if not label:
            return []

        return [
            Attribution(
                address=address,
                chain=chain,
                label=label,
                # A deployment, not an ownership claim. "This contract belongs
                # to the topbidder project" is much narrower than "this address
                # is controlled by X", and reading one as the other is the error
                # this category prevents.
                category=Category.CONTRACT,
                confidence=Confidence.MEDIUM,
                method=Method.LIST,
                source=f"ethereum-lists/contracts ({upstream or 'no source recorded'})",
                rationale=(
                    f"listed as a contract of '{project or 'an unnamed project'}' "
                    f"in a maintainer-reviewed community registry"
                    + (f", which records its own source as '{upstream}'" if upstream else "")
                    + ". A deployment record, not a claim about who controls the "
                    "address"
                ),
            )
        ]


def clone(path: Path | str = "data/labels/contracts") -> int:
    """Shallow-clone the registry. Returns how many contract files landed.

    A clone rather than 60,000 requests: crawling one file per contract is not
    a thing to do to GitHub or to somebody's rate limit, and the repository is
    45 MB at depth 1.

    Separate from the source and never called by it. A lookup that quietly
    fetched would make an offline run behave differently from an online one
    with nothing said.
    """
    target = Path(path)
    if (target / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(target), "pull", "--depth", "1", "--ff-only"],
            check=True,
            capture_output=True,
        )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", REPOSITORY, str(target)],
            check=True,
            capture_output=True,
        )
    return sum(1 for _ in (target / "contracts").rglob("0x*.json"))
