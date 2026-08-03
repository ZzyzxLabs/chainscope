"""Tokens pretending to be other tokens, and the totals they corrupt.

Measured on a real case: a seed address with 55 ERC-20 transfers, of which 48
were impersonation tokens. The six that mattered were the rest. A tool that
answers "how much USDC moved through here" by grouping on the symbol string
answers with a number built almost entirely out of forgeries, and that number is
shaped exactly like a real one --- same units, same magnitude, same place in the
report.

That is the failure this package is arranged against, in the one place where the
attacker is *choosing* the data the tool reads. Nothing else in a case is
adversarially authored: a block timestamp is not trying to mislead. A token
symbol is a string the forger picked, from the full space of Unicode, having
looked at the tool that will render it.

**Three mechanisms, three checks, and no two of them overlap.**

======================  ==========================================  ==================
Symbol                  How it works                                What catches it
======================  ==========================================  ==================
``UЅDC``                Latin word, one Cyrillic letter spliced in  mixed-script
``ЕТН``                 Entirely Cyrillic, so perfectly consistent  confusable skeleton
``ETH``                 Plain ASCII; simply named after a real one  canonical registry
======================  ==========================================  ==================

The third is the one that matters most and the one Unicode cannot touch. A token
contract may legally call itself anything; ``symbol()`` is a string the deployer
chose. So the check that actually decides is the one the field has always used
and `docs/methods` states as a rule: **compare the contract address, never the
symbol string.** :data:`CANONICAL` is that comparison made explicit.

**What this refuses to do.**

It does not delete rows, filter transfers, or adjust totals. It reports, and it
reports both directions --- the forged rows *and* the genuine ones --- because a
tool that silently dropped what it judged fake would be making the same kind of
unreviewable decision as one that silently kept it, and would be much harder to
argue with. §1 of `docs/needs.md`: an answer must state its own completeness.

It also does not treat "not in the registry" as "forged". Most tokens are not in
any registry and are perfectly real. Absence of a canonical entry means the
canonical check had nothing to say, and the finding says so in those words.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..chains import address_key
from ..core.chainid import ChainId
from ..core.confusable import (
    confusable,
    is_mixed_script,
    skeleton,
    suspicious_characters,
)
from ..core.result import Finding, Result, Severity
from ..providers.base import Capability
from .base import Analyzer, Context, history_of

__all__ = [
    "CANONICAL",
    "Impersonation",
    "ImpersonationAnalyzer",
    "Verdict",
    "canonical_for",
    "inspect_assets",
]


#: Contracts that legitimately carry a given symbol, per chain.
#:
#: Deliberately small, and deliberately not a token list. This is not "every
#: token" --- it is the handful whose names are worth forging, which is a much
#: shorter and much more stable list. A general token list would need constant
#: maintenance and would make every unlisted token look suspicious, which
#: inverts the error this module exists to prevent.
#:
#: Keys are ``(chain namespace and reference, uppercased symbol)``. Values are
#: the real contract addresses, lowercased --- these are all EVM chains, where
#: hex folds. A chain whose addresses do not fold case must not be added here
#: without also changing the lookup; see :mod:`chainscope.chains`.
CANONICAL: dict[tuple[str, str], frozenset[str]] = {
    ("eip155:1", "USDT"): frozenset({"0xdac17f958d2ee523a2206206994597c13d831ec7"}),
    ("eip155:1", "USDC"): frozenset({"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}),
    ("eip155:1", "DAI"): frozenset({"0x6b175474e89094c44da98b954eedeac495271d0f"}),
    ("eip155:1", "WETH"): frozenset({"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"}),
    ("eip155:1", "WBTC"): frozenset({"0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"}),
    # ETH on an EVM chain is native and has no contract at all. Any contract
    # claiming the symbol is impersonating by construction --- which is exactly
    # the ASCII case above, and the reason an empty set is a meaningful value
    # here rather than a missing key.
    ("eip155:1", "ETH"): frozenset(),
    ("eip155:56", "USDT"): frozenset({"0x55d398326f99059ff775485246999027b3197955"}),
    ("eip155:56", "USDC"): frozenset({"0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"}),
    ("eip155:56", "BNB"): frozenset(),
    ("eip155:137", "USDT"): frozenset({"0xc2132d05d31c914a87c6611c10748aeb04b58e8f"}),
    ("eip155:137", "USDC"): frozenset({"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"}),
    ("eip155:42161", "USDT"): frozenset({"0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"}),
    ("eip155:42161", "USDC"): frozenset({"0xaf88d065e77c8cc2239327c5edb3a432268e5831"}),
    ("eip155:8453", "USDC"): frozenset({"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"}),
}


class Verdict:
    """Why an asset was reported. A `str` enum with no ordering, on purpose.

    Ranking these would invite ``verdict > SUSPICIOUS`` somewhere, and they are
    not degrees of the same thing --- they are different observations that
    happen to arrive at the same table.
    """

    GENUINE = "genuine"
    """Matches a canonical contract for its symbol. The strongest statement."""

    FORGED = "forged"
    """Claims a canonical symbol from a contract that is not the canonical one."""

    LOOKALIKE = "lookalike"
    """Its symbol resembles a canonical symbol without equalling it."""

    UNKNOWN_SCRIPT = "unknown-script"
    """Its symbol mixes scripts, or contains characters nobody types by hand."""

    UNLISTED = "unlisted"
    """No canonical entry for this symbol. Says nothing either way."""


@dataclass(frozen=True, slots=True)
class Impersonation:
    """One asset, and what is wrong with how it presents itself."""

    chain: ChainId
    contract: str
    symbol: str
    verdict: str
    transfers: int = 0
    resembles: str = ""
    """The canonical symbol this one imitates, when it is not simply equal."""

    reasons: tuple[str, ...] = ()
    """Human-checkable, character by character. See below --- this is the point."""

    characters: tuple[tuple[str, str, str], ...] = ()
    """``(character, codepoint, unicode name)`` for everything non-ASCII.

    Carried so a report can show its work. "This token is a forgery" is an
    assertion; "position 1 is U+0405 CYRILLIC CAPITAL LETTER DZE, which resembles
    S" is something the reader can verify without trusting this code, and that
    difference is what separates a finding from an accusation.
    """

    @property
    def is_impersonation(self) -> bool:
        return self.verdict in (Verdict.FORGED, Verdict.LOOKALIKE, Verdict.UNKNOWN_SCRIPT)


def canonical_for(chain: ChainId | None, symbol: str) -> frozenset[str] | None:
    """The contracts that may legitimately carry ``symbol``, or ``None``.

    ``None`` and ``frozenset()`` mean different things and the difference is the
    whole check. ``None`` is "no opinion, this symbol is not in the registry".
    An empty set is "this symbol belongs to a native asset, so *any* contract
    claiming it is impersonating".
    """
    if chain is None:
        return None
    return CANONICAL.get((str(chain), symbol.strip().upper()))


def _resembled_symbol(chain: ChainId | None, symbol: str) -> str:
    """A canonical symbol on this chain that ``symbol`` imitates but is not.

    Compared by skeleton, so ``ЕТН`` matches ``ETH``. An exact match returns
    empty: that is not a lookalike, it is the same string, and whether it is
    genuine is the registry's question rather than Unicode's.
    """
    folded = skeleton(symbol).strip().upper()
    for namespace, known in CANONICAL:
        if chain is not None and namespace != str(chain):
            continue
        if known != symbol.strip().upper() and confusable(folded, known):
            return known
    return ""


def _classify(chain: ChainId | None, contract: str, symbol: str) -> tuple[str, str, list[str]]:
    """``(verdict, resembles, reasons)`` for one asset.

    Order matters. The canonical check runs first because it is the only one
    that can say *genuine* --- a token whose contract is the real USDC contract
    is real no matter what its symbol looks like, and reporting it as suspicious
    because of some character would be the expensive direction of wrong.
    """
    reasons: list[str] = []
    key = (contract or "").strip().lower()
    known = canonical_for(chain, symbol)

    if known is not None:
        if key and key in known:
            return Verdict.GENUINE, "", [f"{contract} is the canonical {symbol} on {chain}"]
        expected = (
            ", ".join(sorted(known)) if known else "no contract --- it is the native asset"
        )
        return (
            Verdict.FORGED,
            symbol.strip().upper(),
            [
                f"claims the symbol {symbol!r}, which on {chain} belongs to {expected}",
                f"this contract is {contract or 'not recorded'}",
            ],
        )

    resembles = _resembled_symbol(chain, symbol)
    if resembles:
        reasons.append(f"symbol {symbol!r} is not {resembles!r} but renders identically to it")
    if is_mixed_script(symbol):
        found = sorted(
            {name for _, _, name in suspicious_characters(symbol) for name in [name.split()[0]]}
        )
        reasons.append(
            f"symbol mixes scripts ({', '.join(found)} with Latin); "
            f"UTS #39 treats that as deliberate confusion"
        )
        if not resembles:
            return Verdict.UNKNOWN_SCRIPT, "", reasons
    if resembles:
        return Verdict.LOOKALIKE, resembles, reasons

    if suspicious_characters(symbol):
        return (
            Verdict.UNKNOWN_SCRIPT,
            "",
            [f"symbol {symbol!r} contains characters outside ASCII"],
        )
    return Verdict.UNLISTED, "", ["no canonical entry for this symbol; nothing is claimed"]


def inspect_assets(transfers: list[Any], chain: ChainId | None = None) -> list[Impersonation]:
    """Classify every distinct asset appearing in ``transfers``.

    Grouped by **contract**, never by symbol --- grouping by symbol is the bug.
    Two contracts sharing a ticker are two assets, and the whole point is that
    one of them may have chosen that ticker precisely so this function would
    merge them.
    """
    seen: dict[tuple[str, str], int] = defaultdict(int)
    chains: dict[tuple[str, str], ChainId | None] = {}
    for transfer in transfers:
        amount = getattr(transfer, "amount", None)
        symbol = (getattr(amount, "symbol", "") or "").strip()
        asset = getattr(transfer, "asset", None)
        contract = (getattr(asset, "raw", None) or getattr(asset, "key", None) or "") or ""
        key = (str(contract).strip().lower(), symbol)
        seen[key] += 1
        chains.setdefault(key, getattr(transfer, "chain", None) or chain)

    found: list[Impersonation] = []
    for (contract, symbol), count in seen.items():
        where = chains[(contract, symbol)]
        verdict, resembles, reasons = _classify(where, contract, symbol)
        found.append(
            Impersonation(
                chain=where,  # type: ignore[arg-type]
                contract=contract,
                symbol=symbol,
                verdict=verdict,
                transfers=count,
                resembles=resembles,
                reasons=tuple(reasons),
                characters=tuple(suspicious_characters(symbol)),
            )
        )
    # Impersonations first, then by how much of the data each accounts for. A
    # forgery responsible for 48 of 55 rows is a different problem from one
    # responsible for a single dust transfer, and the ordering should say so.
    found.sort(key=lambda i: (not i.is_impersonation, -i.transfers, i.symbol))
    return found


@dataclass
class Report:
    """What `inspect_assets` found, with the arithmetic already done.

    The counts are the finding. "Three forged tokens" is mildly interesting;
    "48 of 55 transfers are forged" is the sentence that changes what somebody
    does next.
    """

    assets: list[Impersonation] = field(default_factory=list)

    @property
    def impersonations(self) -> list[Impersonation]:
        return [a for a in self.assets if a.is_impersonation]

    @property
    def total_transfers(self) -> int:
        return sum(a.transfers for a in self.assets)

    @property
    def forged_transfers(self) -> int:
        return sum(a.transfers for a in self.impersonations)

    @property
    def share(self) -> float:
        """Fraction of transfers belonging to an impersonating asset, 0--1."""
        return 0.0 if not self.total_transfers else self.forged_transfers / self.total_transfers

    def summary(self) -> str:
        if not self.assets:
            return "no assets to inspect"
        if not self.impersonations:
            return (
                f"{len(self.assets)} asset(s), none impersonating a symbol this "
                f"registry knows. That is not the same as none being fake: most "
                f"tokens are in no registry, and the canonical check had nothing "
                f"to say about them"
            )
        return (
            f"{len(self.impersonations)} of {len(self.assets)} assets impersonate "
            f"another, accounting for {self.forged_transfers} of "
            f"{self.total_transfers} transfers ({self.share:.0%}). A total grouped "
            f"by symbol over this data is mostly forgery"
        )


def report(transfers: list[Any], chain: ChainId | None = None) -> Report:
    """Inspect, count, and hand back something that can state its own scale."""
    return Report(assets=inspect_assets(transfers, chain))


def findings(rep: Report) -> list[Finding]:
    """Turn a report into findings, including one for the genuine assets.

    Both directions, deliberately. An investigator who reads only "these three
    are fake" still has to work out which of the remaining rows to trust, and
    the answer to that is the more useful half.
    """
    out: list[Finding] = []
    for asset in rep.impersonations:
        detail = "\n".join(f"  - {r}" for r in asset.reasons)
        if asset.characters:
            shown = ", ".join(f"{ch!r} {code} {name}" for ch, code, name in asset.characters)
            detail += f"\n  - non-ASCII characters: {shown}"
        out.append(
            Finding(
                title=(
                    f"{asset.symbol!r} ({asset.transfers} transfers) impersonates "
                    f"{asset.resembles or 'a real asset'}"
                ),
                # CRITICAL, not IMPORTANT: this is not a detail about the case,
                # it is a statement that some other number in the report is
                # wrong. It has to outrank the number it invalidates.
                severity=Severity.CRITICAL,
                detail=(
                    f"Contract {asset.contract or 'unrecorded'} on {asset.chain}.\n"
                    f"{detail}\n"
                    f"  - any total grouped by symbol has merged this with the real one"
                ),
                data={
                    "contract": asset.contract,
                    "symbol": asset.symbol,
                    "verdict": asset.verdict,
                    "resembles": asset.resembles,
                    "transfers": asset.transfers,
                    "characters": [list(c) for c in asset.characters],
                },
            )
        )

    genuine = [a for a in rep.assets if a.verdict == Verdict.GENUINE]
    if genuine:
        out.append(
            Finding(
                title=f"{len(genuine)} asset(s) confirmed against a canonical contract",
                severity=Severity.INFO,
                detail=(
                    "\n".join(
                        f"  {a.symbol}  {a.contract}  {a.transfers} transfers" for a in genuine
                    )
                    + "\n\nThese are the rows a total may be built from. The check is "
                    "contract identity, not the symbol string."
                ),
                data={
                    "genuine": [{"symbol": a.symbol, "contract": a.contract} for a in genuine]
                },
            )
        )

    unlisted = [a for a in rep.assets if a.verdict == Verdict.UNLISTED]
    if unlisted:
        out.append(
            Finding(
                title=f"{len(unlisted)} asset(s) the registry says nothing about",
                severity=Severity.NOTABLE,
                detail=(
                    "Not a verdict. Most tokens are in no registry and are entirely "
                    "real; the canonical check simply had nothing to compare against. "
                    "Reading this as 'clean' is the error this section exists to "
                    "prevent.\n\n"
                    + "\n".join(
                        f"  {a.symbol or '(no symbol)'}  {a.contract}" for a in unlisted
                    )
                ),
                data={"unlisted": [a.contract for a in unlisted]},
            )
        )
    return out


def analyse(transfers: list[Any], chain: ChainId | None = None) -> Result:
    """The whole pass: inspect, count, and say how much of the data is forged."""
    rep = report(transfers, chain)
    return Result(
        analyzer="impersonation",
        findings=tuple(findings(rep)),
        # The summary rides in `warnings` rather than a `summary` field, because
        # `Result` has none --- and because this *is* a warning. "48 of 55
        # transfers are forged" is not a description of the run, it is a
        # statement that another number in the report cannot be trusted.
        warnings=(
            ()
            if not rep.impersonations
            else (
                rep.summary(),
                f"{rep.share:.0%} of transfers involve an impersonating asset. "
                f"Any figure grouped by symbol is unsafe until these are separated.",
            )
        ),
    )


class ImpersonationAnalyzer(Analyzer):
    """Which of an address's assets are pretending to be other assets."""

    name = "impersonation"
    description = "find tokens whose symbol imitates a real one"
    requires = Capability.ASSET_TRANSFERS

    def applicable(self, ctx: Context) -> bool:
        # Indexer-class. Reconstructing token transfers from traces is possible
        # but slow enough that saying "not applicable" is the honest answer.
        return bool(ctx.router.candidates(ctx.chain, self.requires))

    def run(
        self,
        ctx: Context,
        *,
        address: str = "",
        start_block: int = 0,
        end_block: int | str = "latest",
        **_: Any,
    ) -> Result:
        if not address:
            raise ValueError(
                "impersonation analysis needs an `address` whose assets to inspect"
            )
        started = datetime.now(timezone.utc)
        seed = address_key(ctx.chain, address)
        limit = ctx.limit("per_node", 1000)

        # "all", the value the providers accept. Both directions matter here and
        # the reason is specific to this analyzer: a poisoning token is *sent
        # to* the subject and never sent by it, so the outbound-only
        # enumeration the rest of this package wants would miss the attack
        # entirely --- it would read zero of the 42 forged transfers in the case
        # this was built from.
        transfers, notes = history_of(
            ctx,
            lambda p: p.asset_transfers(
                ctx.chain,
                seed,
                direction="all",
                start_block=start_block,
                end_block=end_block,
                limit=limit,
            ),
        )

        rep = report(list(transfers), ctx.chain)
        warnings = list(notes)
        if len(transfers) >= limit:
            warnings.append(
                f"stopped at {limit} transfers (per_node limit). Assets appearing "
                f"only after that point were not inspected, so the share below is "
                f"over what was read, not over what exists"
            )
        if rep.impersonations:
            warnings.append(rep.summary())
            warnings.append(
                f"{rep.share:.0%} of the transfers read involve an impersonating "
                f"asset. Any figure grouped by symbol is unsafe until these are "
                f"separated by contract"
            )
        return Result(
            analyzer=self.name,
            findings=tuple(findings(rep)),
            warnings=tuple(warnings),
            evidence=ctx.evidence(),
            params={"address": address, "start_block": start_block, "end_block": end_block},
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
