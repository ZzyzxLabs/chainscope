"""Deposit-address consolidation.

The problem this solves: custodial services hand each user a fresh deposit
address, so the addresses funds actually flow to are single-use and appear in no
label database. Looking them up returns nothing, and "nothing" reads as "not a
service" --- which is wrong, and wrong in the direction that makes a trace look
like it ended when it did not.

The structure gives them away. Deposit addresses do not keep funds; they sweep
to a shared wallet. So instead of asking "what is this address", ask "where do
these addresses all send their money", and let the convergence point identify
the service.

A worked shape::

    seed ──> addr A ─┐
    seed ──> addr B ─┼──> 0xf1da…      12 single-use addresses,
    seed ──> addr C ─┤                 one destination
    ...              ─┘

Twelve unlabelled addresses become one entity with twelve deposits. If the hub
carries a public label, you have attribution for the whole group; if it does
not, you still know they are one service and can treat them accordingly.

**When this fails** --- see ``docs/methods/consolidation.md`` for the full list:

* Self-custody with a single wallet produces the same shape for entirely
  innocent reasons.
* Some services reuse one deposit address per user forever, so consecutive
  deposits share an address and fan-in never builds.
* An adversary who knows the technique can send through addresses that never
  consolidate, or consolidate very late.
* Fan-in of two is weak evidence. The default threshold is three.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..core.attribution import Confidence
from ..core.models import Transaction
from ..core.result import Finding, Result, Severity
from ..core.units import Amount
from ..providers.base import Capability, Provider
from .base import Analyzer, Context, history_of

__all__ = ["Cluster", "ConsolidationAnalyzer"]


@dataclass
class Cluster:
    """A set of addresses that all forward to one hub."""

    hub: str
    deposits: list[str] = field(default_factory=list)
    total: int = 0
    """Raw units sent by the seed into this cluster."""

    transfer_count: int = 0
    label: str = ""
    category: str = ""
    confidence: Confidence | None = None

    @property
    def fan_in(self) -> int:
        return len(self.deposits)

    @property
    def attributed(self) -> bool:
        return bool(self.label)


class ConsolidationAnalyzer(Analyzer):
    """Group a seed's counterparties by where those counterparties forward funds."""

    name = "consolidation"
    version = "1.0"
    description = (
        "Identify custodial services by finding where a seed address's "
        "counterparties consolidate their funds"
    )

    def applicable(self, ctx: Context) -> bool:
        # Needs to enumerate an address's history, which plain RPC cannot do.
        return bool(ctx.router.candidates(ctx.chain, Capability.ADDRESS_HISTORY))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        min_fan_in: int = 3,
        start_block: int = 0,
        end_block: int | str = "latest",
        native_symbol: str = "ETH",
        decimals: int = 18,
        **_: Any,
    ) -> Result:
        # Named keyword parameters with defaults, rather than unpacking a dict:
        # they document the analyzer's interface, keep the base class's looser
        # signature substitutable, and let `--list` introspect what can be
        # tuned. Required-ness is enforced here instead of by the signature.
        if not address:
            raise ValueError("consolidation analysis needs an `address` to start from")
        started = datetime.now(timezone.utc)
        seed = address.lower()
        max_dests = ctx.limit("max_nodes", 200)
        per_node = ctx.limit("per_node", 500)

        warnings: list[str] = []
        findings: list[Finding] = []

        history, source_notes = history_of(
            ctx,
            lambda p: p.address_history(
                ctx.chain,
                seed,
                start_block=start_block,
                end_block=end_block,
                limit=per_node,
            ),
        )
        warnings.extend(source_notes)

        outgoing = [
            t
            for t in history
            if t.sender and t.sender.key == seed and t.success and t.value.raw > 0
        ]
        if not outgoing:
            return self._result(
                ctx,
                warnings=("seed address has no outgoing value transfers",),
                params=self._params(address, min_fan_in, start_block, end_block),
                started=started,
            )

        completeness = self._check_completeness(ctx, seed, history, warnings)

        destinations: list[str] = list(
            dict.fromkeys(t.recipient.key for t in outgoing if t.recipient)
        )
        if len(destinations) > max_dests:
            warnings.append(
                f"examined {max_dests} of {len(destinations)} destinations "
                f"(max_nodes limit); clusters beyond that were not detected"
            )
            destinations = destinations[:max_dests]

        # Map each destination to where it forwards. One hop is enough: deposit
        # addresses sweep directly, and going deeper mostly picks up the hub's
        # own downstream traffic.
        hubs: dict[str, list[str]] = defaultdict(list)
        unreachable = 0
        # Collected across every onward fetch and reported once. One line per
        # destination would bury the finding under provider bookkeeping.
        onward_notes: list[str] = []

        def fetch_onward(dest: str) -> list[Transaction]:
            def call(p: Provider) -> list[Transaction]:
                return p.address_history(ctx.chain, dest, limit=per_node)

            # Onward hops are corroborated too: a short answer here understates
            # a destination's reach, and the count is what the finding rests on.
            rows, notes = history_of(ctx, call)
            onward_notes.extend(notes)
            return rows

        def onward_summary() -> None:
            if not onward_notes:
                return
            single = sum(1 for n in onward_notes if "not corroborated" in n)
            if single:
                warnings.append(
                    f"{single} of {len(destinations)} onward lookups came from one "
                    f"source. A destination whose history came back short reads as "
                    f"a narrower hub than it is."
                )
            for note in onward_notes:
                if "not corroborated" not in note:
                    warnings.append(note)

        for dest in destinations:
            try:
                onward = fetch_onward(dest)
            except Exception:
                unreachable += 1
                continue
            for nxt in {
                t.recipient.key
                for t in onward
                if t.sender and t.sender.key == dest and t.recipient and t.value.raw > 0
            }:
                hubs[nxt].append(dest)

        onward_summary()
        if unreachable:
            warnings.append(
                f"{unreachable} destination(s) could not be enumerated; "
                f"clusters they belong to may be undercounted"
            )

        sent_to: dict[str, int] = defaultdict(int)
        count_to: dict[str, int] = defaultdict(int)
        for t in outgoing:
            if t.recipient:
                sent_to[t.recipient.key] += t.value.raw
                count_to[t.recipient.key] += 1

        clusters: list[Cluster] = []
        for hub, members in hubs.items():
            if len(members) < min_fan_in or hub == seed:
                continue
            c = Cluster(
                hub=hub,
                deposits=sorted(members),
                total=sum(sent_to[m] for m in members),
                transfer_count=sum(count_to[m] for m in members),
            )
            if ctx.resolver:
                res = ctx.resolver.resolve(hub, ctx.chain)
                if res.found and res.entity:
                    c.label = res.entity.label
                    c.category = res.entity.category.value
                    c.confidence = res.entity.confidence
                if not res.reliable:
                    warnings.append(
                        f"attribution lookup for {hub} was incomplete; "
                        f"absence of a label is not evidence of absence"
                    )
            clusters.append(c)

        clusters.sort(key=lambda c: (-c.total, -c.fan_in))

        for c in clusters:
            amount = Amount(c.total, decimals, native_symbol)
            if c.attributed:
                title = f"{c.fan_in} deposit addresses consolidate into {c.label}"
                severity = Severity.IMPORTANT
                detail = (
                    f"{amount} sent across {c.transfer_count} transfers to "
                    f"{c.fan_in} single-use addresses, all forwarding to "
                    f"{c.hub} ({c.label})."
                )
            else:
                title = f"{c.fan_in} deposit addresses consolidate into an unlabelled hub"
                severity = Severity.NOTABLE
                detail = (
                    f"{amount} sent across {c.transfer_count} transfers to "
                    f"{c.fan_in} single-use addresses, all forwarding to {c.hub}. "
                    f"The hub carries no public label; the grouping is structural, "
                    f"and identifying the service needs an external source."
                )
            findings.append(
                Finding(
                    title=title,
                    severity=severity,
                    detail=detail,
                    data={
                        "hub": c.hub,
                        "fan_in": c.fan_in,
                        "deposit_addresses": c.deposits,
                        "total_raw": c.total,
                        "total": str(amount),
                        "transfers": c.transfer_count,
                        "label": c.label or None,
                        "category": c.category or None,
                        "confidence": c.confidence.name if c.confidence else None,
                    },
                    evidence=ctx.evidence(),
                )
            )

        clustered = {d for c in clusters for d in c.deposits}
        loose = [d for d in destinations if d not in clustered]
        if loose:
            findings.append(
                Finding(
                    title=f"{len(loose)} destinations did not cluster",
                    severity=Severity.INFO,
                    detail=(
                        "These received funds but share no forwarding hub with "
                        f"at least {min_fan_in - 1} others. They may be relays, "
                        "one-off transfers, or services whose deposit pattern "
                        "this method does not detect."
                    ),
                    data={
                        "addresses": loose[:100],
                        "truncated": max(0, len(loose) - 100),
                        "total_raw": sum(sent_to[d] for d in loose),
                    },
                    evidence=ctx.evidence(),
                )
            )

        if completeness is False:
            warnings.append(
                "transaction history appears incomplete (see finding); totals "
                "below are lower bounds"
            )

        return self._result(
            ctx,
            findings=tuple(findings),
            warnings=tuple(warnings),
            params=self._params(address, min_fan_in, start_block, end_block),
            started=started,
        )

    # ---------------------------------------------------------------- helpers

    def _check_completeness(
        self,
        ctx: Context,
        seed: str,
        history: list[Transaction],
        warnings: list[str],
    ) -> bool | None:
        """Compare the account nonce against how many sends we actually saw.

        Cheap, and the only guard against the quiet failure where a paginated
        history was truncated and every total afterwards is simply too low.
        """
        try:
            account = ctx.router.dispatch(
                ctx.chain,
                Capability.BALANCE,
                lambda p: p.get_account(ctx.chain, seed),
            )
        except Exception:
            warnings.append("could not verify history completeness (no nonce available)")
            return None

        observed = sum(1 for t in history if t.sender and t.sender.key == seed)
        ok = account.completeness_check(observed)
        if ok is False:
            warnings.append(
                f"account nonce is {account.tx_count} but only {observed} outbound "
                f"transactions were retrieved --- history is incomplete"
            )
        return ok

    @staticmethod
    def _params(
        address: str, min_fan_in: int, start_block: int, end_block: int | str
    ) -> dict[str, Any]:
        return {
            "address": address,
            "min_fan_in": min_fan_in,
            "start_block": start_block,
            "end_block": end_block,
        }
