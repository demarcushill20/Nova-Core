"""Decision engine for the skill optimization pipeline.

Implements accept/reject logic with hard gates and explicit reason codes.
This is the core judgment module — all acceptance decisions flow through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
THRESHOLDS_PATH = BASE_DIR / "configs" / "thresholds.yaml"

# Reason codes (machine-readable)
ACCEPT = "ACCEPT"
REJECT_PRECISION = "REJECT_PRECISION"
REJECT_FPR = "REJECT_FPR"
REJECT_NEIGHBOR = "REJECT_NEIGHBOR"
REJECT_SMOKE = "REJECT_SMOKE"
REJECT_GAIN = "REJECT_GAIN"
REJECT_POLICY = "REJECT_POLICY"
REJECT_BROADENING = "REJECT_BROADENING"
REJECT_LENGTH = "REJECT_LENGTH"
REJECT_DATASET = "REJECT_DATASET"
REJECT_VARIANCE = "REJECT_VARIANCE"
SKIP_FROZEN = "SKIP_FROZEN"
SKIP_NO_DATA = "SKIP_NO_DATA"


@dataclass
class Decision:
    """Accept/reject decision for a candidate."""

    skill_name: str
    decision: str  # ACCEPT, REJECT_*, SKIP_*
    reason: str  # human-readable explanation
    reason_code: str  # machine-readable code
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    delta: float = 0.0
    gate_details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_accepted(self) -> bool:
        return self.decision == ACCEPT

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "decision": self.decision,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "delta": round(self.delta, 4),
            "gate_details": self.gate_details,
        }


def load_thresholds() -> dict:
    """Load thresholds configuration."""
    if THRESHOLDS_PATH.exists():
        return yaml.safe_load(THRESHOLDS_PATH.read_text())
    return {}


def _get_precision_floor(risk_level: str, thresholds: dict) -> float:
    """Get the precision floor for a risk level."""
    floors = thresholds.get("hard_gates", {}).get("precision_floor", {})
    return floors.get(risk_level, 0.80)


def _get_risk_override(key: str, risk_level: str, thresholds: dict, default: float) -> float:
    """Get a threshold value, checking risk-level overrides first."""
    overrides = thresholds.get("risk_overrides", {}).get(risk_level, {})
    if key in overrides:
        return overrides[key]
    return thresholds.get("hard_gates", {}).get(key, default)


def evaluate_gates(
    skill_name: str,
    risk_level: str,
    baseline_metrics: dict,
    candidate_metrics: dict,
    candidate_description: str,
    policy_violations: list[str] | None = None,
    neighbor_conflict_rate: float = 0.0,
    smoke_regressions: int = 0,
    hard_negative_regression: bool = False,
    thresholds: dict | None = None,
) -> Decision:
    """Evaluate all hard gates and return a decision.

    Gate evaluation order (short-circuits on first failure):
    1. Policy violations
    2. Description length
    3. Precision floor
    4. FPR regression
    5. Neighbor conflict
    6. Smoke regression
    7. Anti-broadening
    8. Minimum gain

    Args:
        skill_name: Name of the skill being evaluated
        risk_level: Skill's risk level (low/medium/high/critical)
        baseline_metrics: Metrics dict from baseline evaluation
        candidate_metrics: Metrics dict from candidate evaluation
        candidate_description: The proposed description text
        policy_violations: List of policy violation messages (from candidate_generator)
        neighbor_conflict_rate: Measured neighbor conflict rate
        smoke_regressions: Count of global smoke test regressions
        hard_negative_regression: Whether hard negatives got worse
        thresholds: Thresholds config (loaded if not provided)
    """
    if thresholds is None:
        thresholds = load_thresholds()

    gates = thresholds.get("hard_gates", {})
    gate_details: dict[str, Any] = {}

    baseline_score = baseline_metrics.get("weighted_score", 0.0)
    candidate_score = candidate_metrics.get("weighted_score", 0.0)
    delta = candidate_score - baseline_score

    # Gate 1: Policy violations
    if policy_violations:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_POLICY,
            reason=f"Policy violations: {'; '.join(policy_violations)}",
            reason_code=REJECT_POLICY,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details={"violations": policy_violations},
        )

    # Gate 2: Description length
    max_len = gates.get("max_description_length", 1024)
    if len(candidate_description) > max_len:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_LENGTH,
            reason=f"Description too long: {len(candidate_description)} > {max_len}",
            reason_code=REJECT_LENGTH,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details={"length": len(candidate_description), "max": max_len},
        )

    # Gate 3: Precision floor
    precision_floor = _get_precision_floor(risk_level, thresholds)
    candidate_precision = candidate_metrics.get("precision", 0.0)
    gate_details["precision"] = {
        "value": candidate_precision,
        "floor": precision_floor,
        "passed": candidate_precision >= precision_floor,
    }
    if candidate_precision < precision_floor:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_PRECISION,
            reason=f"Precision {candidate_precision:.3f} below floor {precision_floor:.2f} for {risk_level} risk",
            reason_code=REJECT_PRECISION,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # Gate 4: FPR regression
    fpr_limit = _get_risk_override("fpr_regression_limit", risk_level, thresholds, 0.05)
    baseline_fpr = baseline_metrics.get("fpr", 0.0)
    candidate_fpr = candidate_metrics.get("fpr", 0.0)
    fpr_delta = candidate_fpr - baseline_fpr
    gate_details["fpr"] = {
        "baseline": baseline_fpr,
        "candidate": candidate_fpr,
        "delta": round(fpr_delta, 4),
        "limit": fpr_limit,
        "passed": fpr_delta <= fpr_limit,
    }
    if fpr_delta > fpr_limit:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_FPR,
            reason=f"FPR regression {fpr_delta:.3f} exceeds limit {fpr_limit:.2f}",
            reason_code=REJECT_FPR,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # Gate 5: Neighbor conflict
    conflict_limit = _get_risk_override("neighbor_conflict_limit", risk_level, thresholds, 0.10)
    gate_details["neighbor"] = {
        "conflict_rate": neighbor_conflict_rate,
        "limit": conflict_limit,
        "passed": neighbor_conflict_rate <= conflict_limit,
    }
    if neighbor_conflict_rate > conflict_limit:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_NEIGHBOR,
            reason=f"Neighbor conflict rate {neighbor_conflict_rate:.3f} exceeds limit {conflict_limit:.2f}",
            reason_code=REJECT_NEIGHBOR,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # Gate 6: Smoke regression (zero tolerance)
    smoke_tolerance = gates.get("smoke_regression_tolerance", 0)
    gate_details["smoke"] = {
        "regressions": smoke_regressions,
        "tolerance": smoke_tolerance,
        "passed": smoke_regressions <= smoke_tolerance,
    }
    if smoke_regressions > smoke_tolerance:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_SMOKE,
            reason=f"Global smoke regressions: {smoke_regressions} (tolerance: {smoke_tolerance})",
            reason_code=REJECT_SMOKE,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # Gate 7: Anti-broadening
    gate_details["anti_broadening"] = {
        "hard_negative_regression": hard_negative_regression,
        "passed": not hard_negative_regression,
    }
    if hard_negative_regression:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_BROADENING,
            reason="Hard negatives regressed — candidate may be broadening scope",
            reason_code=REJECT_BROADENING,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # Gate 8: Minimum gain
    min_gain = _get_risk_override("min_score_improvement", risk_level, thresholds, 0.02)
    gate_details["gain"] = {
        "delta": round(delta, 4),
        "min_required": min_gain,
        "passed": delta >= min_gain,
    }
    if delta < min_gain:
        return Decision(
            skill_name=skill_name,
            decision=REJECT_GAIN,
            reason=f"Score improvement {delta:.4f} below minimum {min_gain:.2f}",
            reason_code=REJECT_GAIN,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            gate_details=gate_details,
        )

    # All gates passed
    return Decision(
        skill_name=skill_name,
        decision=ACCEPT,
        reason=f"All gates passed. Score: {baseline_score:.3f} → {candidate_score:.3f} (+{delta:.4f})",
        reason_code=ACCEPT,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        delta=delta,
        gate_details=gate_details,
    )


class BatchHaltMonitor:
    """Monitors batch-level halt conditions."""

    def __init__(self, thresholds: dict | None = None):
        if thresholds is None:
            thresholds = load_thresholds()
        halt_cfg = thresholds.get("batch_halt", {})
        self.max_consecutive_failures = halt_cfg.get("max_consecutive_failures", 5)
        self.min_acceptance_rate = halt_cfg.get("min_acceptance_rate", 0.10)

        self._consecutive_failures = 0
        self._total_decisions = 0
        self._total_accepts = 0
        self._halted = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def record_decision(self, decision: Decision) -> None:
        """Record a decision and check halt conditions."""
        self._total_decisions += 1

        if decision.is_accepted:
            self._total_accepts += 1
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

        # Check consecutive failures
        if self._consecutive_failures >= self.max_consecutive_failures:
            self._halted = True
            self._halt_reason = (
                f"Max consecutive failures reached: {self._consecutive_failures} >= {self.max_consecutive_failures}"
            )

        # Check acceptance rate (only after enough decisions)
        if self._total_decisions >= 5:
            rate = self._total_accepts / self._total_decisions
            if rate < self.min_acceptance_rate:
                self._halted = True
                self._halt_reason = (
                    f"Acceptance rate too low: "
                    f"{rate:.2f} < {self.min_acceptance_rate:.2f} "
                    f"({self._total_accepts}/{self._total_decisions})"
                )

    def summary(self) -> dict:
        return {
            "total_decisions": self._total_decisions,
            "total_accepts": self._total_accepts,
            "consecutive_failures": self._consecutive_failures,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }
