"""NovaTrade evaluation framework — sacred models, gates, and fitness scoring.

This package contains the "sacred" evaluation infrastructure that the
AutoResearch agent cannot modify. All modules are versioned and hash-tracked
to ensure backtest reproducibility.

Sacred modules (P0.2):
  - fill_model: How orders are filled in simulation
  - spread_model: Spread assumptions per pair/session
  - slippage_model: Slippage assumptions
  - commission_model: Commission structure
  - session_calendar: Trading session hours and weekend gaps
  - intrabar_policy: Same-bar ambiguity resolution (P0.4)

Evaluation modules:
  - gates: Stage A/B/C validity, realism, and robustness checks
  - fitness: Scout score and promotion score computation
"""

from __future__ import annotations

from novatrade.evaluation.commission_model import (
    DEFAULT_COMMISSION_MODEL,
    CommissionModelConfig,
)
from novatrade.evaluation.fill_model import (
    DEFAULT_FILL_MODEL,
    FillModelConfig,
)
from novatrade.evaluation.fitness import (
    FitnessResult,
    compute_promotion_score,
    compute_scout_score,
)
from novatrade.evaluation.gates import (
    GateConfig,
    GateResult,
    GateResults,
    evaluate_stage_a,
    evaluate_stage_b,
    evaluate_stage_c_stub,
)
from novatrade.evaluation.intrabar_policy import (
    DEFAULT_INTRABAR_POLICY,
    AmbiguityResolution,
    IntrabarPolicyConfig,
)
from novatrade.evaluation.reproducibility_manifest import (
    ReproducibilityManifest,
    compare_manifests,
    create_manifest,
    load_manifest,
    save_manifest,
)
from novatrade.evaluation.sacred_registry import (
    SacredBundle,
    collect_sacred_hashes,
    verify_sacred_unchanged,
)
from novatrade.evaluation.session_calendar import (
    DEFAULT_SESSION_CALENDAR,
    Session,
    SessionCalendarConfig,
)
from novatrade.evaluation.slippage_model import (
    DEFAULT_SLIPPAGE_MODEL,
    SlippageModelConfig,
)
from novatrade.evaluation.spread_model import (
    DEFAULT_SPREAD_MODEL,
    SpreadModelConfig,
)

__all__ = [
    "DEFAULT_COMMISSION_MODEL",
    "DEFAULT_FILL_MODEL",
    "DEFAULT_INTRABAR_POLICY",
    "DEFAULT_SESSION_CALENDAR",
    "DEFAULT_SLIPPAGE_MODEL",
    "DEFAULT_SPREAD_MODEL",
    # Intrabar policy
    "AmbiguityResolution",
    # Commission model
    "CommissionModelConfig",
    # Fill model
    "FillModelConfig",
    # Fitness
    "FitnessResult",
    "GateConfig",
    # Gates
    "GateResult",
    "GateResults",
    "IntrabarPolicyConfig",
    # Reproducibility manifest
    "ReproducibilityManifest",
    # Sacred registry
    "SacredBundle",
    # Session calendar
    "Session",
    "SessionCalendarConfig",
    # Slippage model
    "SlippageModelConfig",
    # Spread model
    "SpreadModelConfig",
    "collect_sacred_hashes",
    "compare_manifests",
    "compute_promotion_score",
    "compute_scout_score",
    "create_manifest",
    "evaluate_stage_a",
    "evaluate_stage_b",
    "evaluate_stage_c_stub",
    "load_manifest",
    "save_manifest",
    "verify_sacred_unchanged",
]
