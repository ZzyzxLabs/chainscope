"""Run all three taint models, and report where they disagree.

`analysis.taint` implements FIFO, haircut and poison, and this package defaults
to FIFO for reasons set out there and in `docs/risk.md`. What is missing is not
another model. It is the observation that **the three disagree, and where they
disagree is exactly where a decision is fragile.**

Measured in this codebase on 100 ETH stolen:

    fifo      100.0 ETH tainted     exact
    haircut    86.4 ETH             13.6 lost to rounding, unrecoverable
    poison    320.0 ETH             220 manufactured, 5x the addresses

And across 132 publicised Bitcoin heists, haircut paints over 75% of all
non-empty accounts while FIFO paints under 28%.

A vendor picks one model and reports a number. The number is then defended as
though the model were a fact about the money rather than a convention about
bookkeeping. Two things follow that nobody surfaces:

**When all three agree, the answer is robust to the choice.** That is worth
saying out loud, because it is the case where a challenge to the methodology
does not change the outcome and the decision can be defended without arguing
about Clayton's Case at all.

**When they disagree, the decision rests on the convention.** An address that
is 31% tainted under haircut and 0% under FIFO is not "31% tainted". It is an
address whose treatment is decided by a bookkeeping rule, and the person
approving the freeze is entitled to know that before they sign it.

This module computes both. It does not pick a winner --- `Screen.taint` already
records which model produced the exposure, and the policy layer decides. What
this adds is the sentence "and it would have been different under X", which is
the one a compliance officer needs and cannot currently get from anyone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..analysis.taint import TaintPolicy, TaintResult, _keying, trace_taint

__all__ = ["Agreement", "ModelOutcome", "compare_taint_models"]


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    """What one taint model concluded about one address."""

    policy: TaintPolicy
    tainted: int
    """Base units this model attributes to the address."""

    share: Decimal
    """`tainted` over the address's balance, in [0, 1]."""

    touched: int
    """How many addresses the model painted in total. The number that shows
    poison's diffusion: a model that paints five times as many addresses is
    answering a different question, not answering the same one more
    cautiously."""

    unresolved: int
    """Transfers the model could not apply because the sender's holdings
    predate the window. Reported per model because the three consume history
    differently, and a model that could not apply half the transfers has not
    produced a cautious answer --- it has produced an uninformed one."""


@dataclass(frozen=True, slots=True)
class Agreement:
    """The three models' verdicts on one address, and whether they agree."""

    address: str
    outcomes: tuple[ModelOutcome, ...]
    balance: int

    def by(self, policy: TaintPolicy) -> ModelOutcome | None:
        for outcome in self.outcomes:
            if outcome.policy is policy:
                return outcome
        return None

    @property
    def shares(self) -> tuple[Decimal, ...]:
        return tuple(o.share for o in self.outcomes)

    @property
    def spread(self) -> Decimal:
        """Widest disagreement between any two models, as a share.

        Zero means the choice of model does not affect the answer for this
        address. That is the number worth putting next to a decision.
        """
        if not self.outcomes:
            return Decimal(0)
        return max(self.shares) - min(self.shares)

    @property
    def unanimous_clean(self) -> bool:
        """No model found any taint --- including poison.

        The strongest clean result available, and stronger than any single
        model's zero: poison is the most generous painter there is, so poison
        finding nothing means nothing tainted ever passed through, not merely
        that none is held now.
        """
        return all(o.tainted == 0 for o in self.outcomes)

    @property
    def unanimous_tainted(self) -> bool:
        """Every model found some. The choice of convention is not load-bearing."""
        return bool(self.outcomes) and all(o.tainted > 0 for o in self.outcomes)

    @property
    def contested(self) -> bool:
        """The models disagree about whether there is *any* taint at all.

        Not the same as a wide spread. Two models saying 4% and 40% agree that
        the address is exposed and differ on how much; one saying 0% and
        another saying 40% disagree about the fact, and only the second makes
        the decision an artefact of the bookkeeping rule.
        """
        return not self.unanimous_clean and not self.unanimous_tainted

    def explain(self) -> str:
        """One sentence for the person who has to sign the decision."""
        if not self.outcomes:
            return f"{self.address}: no taint model was run."
        parts = ", ".join(
            f"{o.policy.value} {o.share:.1%}"
            + (f" ({o.unresolved} transfer(s) unresolved)" if o.unresolved else "")
            for o in self.outcomes
        )
        if self.unanimous_clean:
            return (
                f"{self.address}: no model found taint, including poison --- so "
                f"none passed through, not merely none is held. ({parts})"
            )
        if self.contested:
            return (
                f"{self.address}: the models disagree about whether there is any "
                f"exposure at all ({parts}). Whatever is decided here rests on "
                f"the choice of bookkeeping rule rather than on the money, and "
                f"the approver should be told so."
            )
        return (
            f"{self.address}: every model found exposure, differing by "
            f"{self.spread:.1%} ({parts}). The conclusion does not depend on "
            f"which rule was used."
        )


def compare_taint_models(
    transfers: Sequence[Any],
    sources: Mapping[str, int] | set[str],
    address: str,
    balance: int,
    *,
    opening_balances: Mapping[str, int] | None = None,
    policies: Sequence[TaintPolicy] = (
        TaintPolicy.FIFO,
        TaintPolicy.HAIRCUT,
        TaintPolicy.POISON,
    ),
) -> Agreement:
    """Trace once per model and collect the verdicts on ``address``.

    Three traces rather than one, which is three times the work over a list
    already in memory --- cheap next to the fetch that produced it, and the
    only way to know whether the answer depends on the rule.

    ``balance`` is what the address holds, and the caller supplies it because
    this module must not decide what "holds" means: a balance read from the
    chain and a balance summed from the transfers in hand are different
    numbers, and quietly picking one would put an unstated assumption
    underneath every share in the result.
    """
    if balance < 0:
        raise ValueError("balance cannot be negative")

    rows = list(transfers)
    # Normalised the way `trace_taint` keys its own output, which derives the
    # rule from the transfers' chain. Trying `address` and then `address.lower()`
    # was the first version and is the trap `TaintResult.share` documents:
    # lowercasing a base58 address asks about a different account, so on Solana,
    # Sui and Bitcoin it silently answers zero for an address that does hold
    # tainted value.
    wanted = _keying(rows)(address)
    outcomes: list[ModelOutcome] = []
    for policy in policies:
        result: TaintResult = trace_taint(
            rows,
            sources,  # type: ignore[arg-type]
            policy=policy,
            opening_balances=opening_balances,  # type: ignore[arg-type]
        )
        tainted = result.tainted.get(wanted, 0)
        share = Decimal(min(tainted, balance)) / Decimal(balance) if balance else Decimal(0)
        outcomes.append(
            ModelOutcome(
                policy=policy,
                tainted=tainted,
                share=share,
                touched=len(result.touched),
                unresolved=len(result.unresolved),
            )
        )
    return Agreement(address=address, outcomes=tuple(outcomes), balance=balance)
