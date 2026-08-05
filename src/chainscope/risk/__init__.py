"""Deposit screening: what value is exposed to, and what to do about it.

See `docs/risk.md` for the design and the literature it rests on. The short
version of the shape: `screener` reads a store and walks the money backwards,
`exposure` describes what it found, `policy` decides, `decision` records ---
and none of them is allowed to collapse into a score.
"""

from .agreement import Agreement, ModelOutcome, compare_taint_models
from .decision import Action, Counterfactual, Decision, DecisionError
from .exposure import Directness, Exposure, ExposureError, Screen, Signal, StopReason
from .policy import Match, Policy, PolicyError, Rule, When
from .rules import load_policy, starter_policy
from .screener import Reached, screen

__all__ = [
    "Action",
    "Agreement",
    "Counterfactual",
    "Decision",
    "DecisionError",
    "Directness",
    "Exposure",
    "ExposureError",
    "Match",
    "ModelOutcome",
    "Policy",
    "PolicyError",
    "Reached",
    "Rule",
    "Screen",
    "Signal",
    "StopReason",
    "When",
    "compare_taint_models",
    "load_policy",
    "screen",
    "starter_policy",
]
