"""Deposit screening: what value is exposed to, and what to do about it.

See `docs/risk.md` for the design and the literature it rests on. The short
version of the shape: `exposure` describes, `policy` decides, `decision`
records --- and none of the three is allowed to collapse into a score.
"""

from .agreement import Agreement, ModelOutcome, compare_taint_models
from .decision import Action, Counterfactual, Decision, DecisionError
from .exposure import Directness, Exposure, ExposureError, Screen, Signal, StopReason

__all__ = [
    "Action",
    "Agreement",
    "Counterfactual",
    "Decision",
    "DecisionError",
    "Directness",
    "Exposure",
    "ExposureError",
    "ModelOutcome",
    "Screen",
    "Signal",
    "StopReason",
    "compare_taint_models",
]
