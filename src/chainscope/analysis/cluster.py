"""Common-input-ownership clustering.

The oldest and most reliable heuristic in Bitcoin forensics, and the basis of
every commercial clustering product: **to spend several inputs in one
transaction, you must sign for all of them**, so those addresses are controlled
by one party.

Applied transitively, it expands one address into a wallet. That is often the
single most valuable step in a UTXO investigation --- it turns "this address
received 2 BTC" into "this entity holds 340 BTC across 89 addresses".

Two things make it more delicate than it sounds.

**CoinJoin inverts it.** In a collaborative transaction, inputs belong to
*different* people by design. Applying the heuristic there does not merely add
noise --- it merges unrelated parties into one cluster, and because the merge is
transitive, one CoinJoin can poison an entire result. Detection is therefore not
optional here, and suspected CoinJoins are excluded rather than down-weighted.

**A cluster is not an identity.** It shows that addresses share a controller.
It does not say who, and it cannot distinguish a person from a custodian holding
funds for thousands of users --- which is why exchange clusters are enormous and
why walking into one usually ends a trace rather than advancing it.

See ``docs/methods/clustering.md``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..core.result import Finding, Result, Severity
from .base import Analyzer, Context

__all__ = ["ClusterResult", "CoSpendClusterAnalyzer", "looks_like_coinjoin"]

#: Equal-valued outputs needed before a transaction is treated as a CoinJoin.
#: Five is conservative: ordinary transactions occasionally produce two or three
#: equal outputs, but rarely five, and a false positive here costs only a
#: skipped transaction while a false negative merges strangers.
COINJOIN_EQUAL_OUTPUTS = 5


def looks_like_coinjoin(
    input_count: int, output_values: list[int], threshold: int = COINJOIN_EQUAL_OUTPUTS
) -> bool:
    """Whether a transaction has the shape of a collaborative spend.

    Detects the equal-output signature common to Wasabi, JoinMarket, and
    Whirlpool. Deliberately errs toward suspicion: skipping a real transaction
    loses a little coverage, whereas clustering through a real CoinJoin merges
    unrelated people and propagates transitively.
    """
    if input_count < threshold:
        return False
    counts: dict[int, int] = {}
    for v in output_values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.values(), default=0) >= threshold


@dataclass
class ClusterResult:
    """A set of addresses inferred to share one controller."""

    seed: str
    addresses: set[str] = field(default_factory=set)
    transactions: set[str] = field(default_factory=set)
    skipped_coinjoins: set[str] = field(default_factory=set)
    truncated: bool = False
    """True when a limit stopped the expansion.

    Matters for interpretation: a truncated cluster is a lower bound on the
    wallet, and describing it as "the wallet" would understate holdings.
    """

    @property
    def size(self) -> int:
        return len(self.addresses)


class CoSpendClusterAnalyzer(Analyzer):
    """Expand an address into the wallet that controls it."""

    name = "co-spend-cluster"
    version = "1.0"
    description = "Group addresses controlled by one party via common-input ownership"

    def __init__(self, walker: Any = None) -> None:
        self.walker = walker
        """Object exposing ``spending_transactions(address)``, each with
        ``txid``, ``input_addresses``, and ``output_values``."""

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        max_addresses: int = 1000,
        max_transactions: int = 500,
        skip_coinjoins: bool = True,
        **_: Any,
    ) -> Result:
        started = datetime.now(timezone.utc)
        if not address:
            raise ValueError("clustering needs an `address` to start from")
        if self.walker is None:
            raise ValueError("no walker configured for this chain")

        cluster = ClusterResult(seed=address, addresses={address})
        warnings: list[str] = []
        queue: deque[str] = deque([address])
        seen_addresses = {address}
        scanned = 0

        while queue and cluster.size < max_addresses and scanned < max_transactions:
            current = queue.popleft()
            try:
                spends = self.walker.spending_transactions(current)
            except Exception as exc:
                warnings.append(f"could not enumerate {current}: {exc}")
                continue

            for tx in spends:
                scanned += 1
                if scanned > max_transactions:
                    cluster.truncated = True
                    break
                if tx.txid in cluster.transactions:
                    continue

                inputs = [a for a in tx.input_addresses if a]
                # The heuristic only holds when this address is an *input*.
                # Receiving funds says nothing about who controls the sender.
                if current not in inputs:
                    continue

                if skip_coinjoins and looks_like_coinjoin(len(inputs), tx.output_values):
                    cluster.skipped_coinjoins.add(tx.txid)
                    continue

                cluster.transactions.add(tx.txid)
                for a in inputs:
                    if a not in seen_addresses:
                        seen_addresses.add(a)
                        cluster.addresses.add(a)
                        if cluster.size < max_addresses:
                            queue.append(a)
                        else:
                            # Discovered but never expanded. Without this flag
                            # the walk ends with an empty queue and looks
                            # complete -- the exact silent truncation this
                            # analyzer is supposed to make impossible.
                            cluster.truncated = True

        if queue or scanned >= max_transactions:
            cluster.truncated = True

        if cluster.truncated:
            warnings.append(
                f"expansion stopped at {cluster.size} addresses / {scanned} "
                f"transactions (limits). This cluster is a lower bound on the "
                f"wallet, not its full extent."
            )
        if cluster.skipped_coinjoins:
            warnings.append(
                f"skipped {len(cluster.skipped_coinjoins)} suspected CoinJoin "
                f"transaction(s). Clustering through one would merge unrelated "
                f"parties, and the merge propagates transitively."
            )

        findings = [
            Finding(
                title=f"cluster of {cluster.size} address(es)",
                severity=Severity.NOTABLE if cluster.size > 1 else Severity.INFO,
                detail=(
                    f"{cluster.size} addresses appear as co-inputs across "
                    f"{len(cluster.transactions)} transaction(s), so one party "
                    f"controls all of them. This shows shared control; it does "
                    f"not identify the controller, and a large cluster is more "
                    f"likely a custodian than an individual."
                ),
                data={
                    "seed": address,
                    "size": cluster.size,
                    "addresses": sorted(cluster.addresses)[:500],
                    "address_overflow": max(0, cluster.size - 500),
                    "transactions": len(cluster.transactions),
                    "skipped_coinjoins": sorted(cluster.skipped_coinjoins),
                    "truncated": cluster.truncated,
                },
                evidence=ctx.evidence(),
            )
        ]

        if cluster.size > 10_000:
            findings.append(
                Finding(
                    title="cluster is service-scale",
                    severity=Severity.IMPORTANT,
                    detail=(
                        f"{cluster.size} addresses is far beyond individual use "
                        f"and indicates a custodial service. Tracing normally "
                        f"stops here: funds inside a custodian's wallet are no "
                        f"longer distinguishable on-chain."
                    ),
                    data={"size": cluster.size},
                    evidence=ctx.evidence(),
                )
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params={
                "address": address,
                "max_addresses": max_addresses,
                "max_transactions": max_transactions,
                "skip_coinjoins": skip_coinjoins,
            },
            started=started,
        )
