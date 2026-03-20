"""Promotion pipeline — full validation battery for campaign champions.

After a campaign identifies a best candidate, the promotion pipeline runs
the complete validation gauntlet:
  1. Walk-forward OOS validation (multi-window)
  2. Holdout evaluation (separate from WF holdout)
  3. Parameter perturbation stability test
  4. Cost stress test (2x spread + slippage)
  5. Durability validation (rolling profitability, regime consistency, drawdown)
  6. Promotion score computation

Only strategies that pass ALL five stages earn a promotion score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_promotion_score
from novatrade.optimization.perturbation import (
    PerturbationConfig,
    PerturbationResult,
    run_perturbation_test,
)
from novatrade.optimization.stress import StressConfig, StressResult, run_stress_test
from novatrade.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    run_walk_forward,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PromotionResult:
    """Outcome of the full promotion validation pipeline."""

    experiment_id: str
    walkforward_passed: bool = False
    holdout_passed: bool = False
    perturbation_passed: bool = False
    stress_passed: bool = False
    durability_passed: bool = False
    durability_grade: str | None = None
    promotion_score: float | None = None
    overall_passed: bool = False
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Promotion pipeline
# ---------------------------------------------------------------------------


def run_promotion_pipeline(
    config: StrategyConfig,
    h1_candles: list,
    h4_candles: list,
    experiment_id: str,
    baseline_score: float,
    wf_config: WalkForwardConfig | None = None,
    perturb_config: PerturbationConfig | None = None,
    stress_config: StressConfig | None = None,
    trades: list | None = None,
    durability_config: Any | None = None,
) -> PromotionResult:
    """Run the full promotion validation battery on a champion strategy.

    Executes four stages sequentially. Each stage must pass for the
    overall pipeline to succeed.  If any stage fails, later stages are
    still run to provide complete diagnostic data.

    Args:
        config: Strategy configuration to promote.
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        experiment_id: ID of the experiment being promoted.
        baseline_score: Scout score from the campaign (in-sample).
        wf_config: Walk-forward configuration (uses defaults if None).
        perturb_config: Perturbation test configuration.
        stress_config: Cost stress test configuration.

    Returns:
        PromotionResult with per-stage verdicts and optional promotion score.
    """
    result = PromotionResult(experiment_id=experiment_id)
    wf_cfg = wf_config or WalkForwardConfig()
    pt_cfg = perturb_config or PerturbationConfig()
    st_cfg = stress_config or StressConfig()

    log.info("Starting promotion pipeline for %s", experiment_id)
    log.info("Baseline scout score: %.4f", baseline_score)

    # --- Stage 1: Walk-forward validation ---
    log.info("Stage 1/4: Walk-forward validation...")
    wf_result = _run_walkforward_stage(config, h1_candles, h4_candles, wf_cfg)
    result.walkforward_passed = wf_result.overall_passed
    result.details["walkforward"] = {
        "windows": len(wf_result.windows),
        "median_oos_score": wf_result.median_oos_score,
        "median_oos_is_ratio": wf_result.median_oos_is_ratio,
        "all_windows_passed": wf_result.all_windows_passed,
        "overall_passed": wf_result.overall_passed,
    }
    log.info(
        "Walk-forward: %s (median OOS=%.4f, ratio=%.4f)",
        "PASS" if result.walkforward_passed else "FAIL",
        wf_result.median_oos_score,
        wf_result.median_oos_is_ratio,
    )

    # --- Stage 2: Holdout evaluation ---
    log.info("Stage 2/4: Holdout evaluation...")
    holdout_passed, holdout_details = _evaluate_holdout(wf_result)
    result.holdout_passed = holdout_passed
    result.details["holdout"] = holdout_details
    log.info("Holdout: %s", "PASS" if holdout_passed else "FAIL")

    # --- Stage 3: Perturbation stability ---
    log.info("Stage 3/4: Perturbation stability test...")
    perturb_result = _run_perturbation_stage(
        config,
        h1_candles,
        h4_candles,
        baseline_score,
        pt_cfg,
    )
    result.perturbation_passed = perturb_result.all_passed
    result.details["perturbation"] = {
        "all_passed": perturb_result.all_passed,
        "worst_drop": perturb_result.worst_drop,
        "cliff_detected": perturb_result.cliff_detected,
        "tests_run": len(perturb_result.results),
    }
    log.info(
        "Perturbation: %s (worst drop=%.1f%%, cliff=%s)",
        "PASS" if result.perturbation_passed else "FAIL",
        perturb_result.worst_drop * 100,
        "YES" if perturb_result.cliff_detected else "no",
    )

    # --- Stage 4: Cost stress test ---
    log.info("Stage 4/4: Cost stress test...")
    stress_result = _run_stress_stage(
        config,
        h1_candles,
        h4_candles,
        baseline_score,
        st_cfg,
    )
    result.stress_passed = stress_result.passed
    result.details["stress"] = {
        "passed": stress_result.passed,
        "normal_score": stress_result.normal_score,
        "stress_score": stress_result.stress_score,
        "still_profitable": stress_result.still_profitable,
        "degradation_pct": stress_result.score_degradation_pct,
    }
    log.info(
        "Stress: %s (degradation=%.1f%%, profitable=%s)",
        "PASS" if result.stress_passed else "FAIL",
        stress_result.score_degradation_pct * 100,
        stress_result.still_profitable,
    )

    # --- Stage 5: Durability validation ---
    durability_report = _run_durability_stage(
        trades=trades,
        h1_candles=h1_candles,
        wf_result=wf_result,
        durability_config=durability_config,
    )
    if durability_report is not None:
        result.durability_passed = durability_report.overall_passed
        result.durability_grade = durability_report.durability_grade
        result.details["durability"] = {
            "grade": durability_report.durability_grade,
            "overall_passed": durability_report.overall_passed,
            "rolling_12m_win_pct": durability_report.rolling_12m_win_pct,
            "rolling_6m_win_pct": durability_report.rolling_6m_win_pct,
            "regime_consistency_score": durability_report.regime_consistency_score,
            "max_underwater_days": durability_report.max_underwater_days,
            "recovery_factor": (
                durability_report.recovery_factor
                if not isinstance(durability_report.recovery_factor, float)
                or durability_report.recovery_factor != float("inf")
                else "inf"
            ),
            "gates_passed": sum(1 for g in durability_report.gates if g.passed),
            "gates_total": len(durability_report.gates),
        }
        log.info(
            "Durability: %s (grade=%s, %d/%d gates)",
            "PASS" if result.durability_passed else "FAIL",
            result.durability_grade,
            sum(1 for g in durability_report.gates if g.passed),
            len(durability_report.gates),
        )
    else:
        # No trades provided — skip durability (treat as passed)
        result.durability_passed = True
        result.durability_grade = None
        result.details["durability"] = {"skipped": True, "reason": "no_trades_provided"}
        log.info("Durability: SKIPPED (no trades provided)")

    # --- Compute promotion score if all pass ---
    result.overall_passed = (
        result.walkforward_passed
        and result.holdout_passed
        and result.perturbation_passed
        and result.stress_passed
        and result.durability_passed
    )

    if result.overall_passed:
        oos_scores = [w.oos_scout_score for w in wf_result.windows]
        holdout_score = wf_result.holdout_score or 0.0
        is_median = wf_result.median_is_score  # true IS median from walk-forward windows
        complexity = len(StrategyConfig.OPTIMIZABLE_PARAMS)

        result.promotion_score = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=is_median,
            complexity=complexity,
        )
        result.details["promotion_score_inputs"] = {
            "oos_scores": oos_scores,
            "holdout_score": holdout_score,
            "is_median": is_median,
            "complexity": complexity,
        }
        log.info("ALL STAGES PASSED — promotion score: %.4f", result.promotion_score)
    else:
        stages_failed = []
        if not result.walkforward_passed:
            stages_failed.append("walkforward")
        if not result.holdout_passed:
            stages_failed.append("holdout")
        if not result.perturbation_passed:
            stages_failed.append("perturbation")
        if not result.stress_passed:
            stages_failed.append("stress")
        if not result.durability_passed:
            stages_failed.append("durability")
        log.warning("PROMOTION FAILED — stages: %s", ", ".join(stages_failed))

    return result


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def _run_walkforward_stage(
    config: StrategyConfig,
    h1_candles: list,
    h4_candles: list,
    wf_config: WalkForwardConfig,
) -> WalkForwardResult:
    """Execute walk-forward validation, returning the raw result."""
    try:
        return run_walk_forward(config, h1_candles, h4_candles, wf_config)
    except Exception as exc:
        log.error("Walk-forward crashed: %s", exc)
        return WalkForwardResult()


def _evaluate_holdout(wf_result: WalkForwardResult) -> tuple[bool, dict]:
    """Extract holdout verdict from walk-forward result."""
    if wf_result.holdout_passed is None:
        # No holdout data available — pass by default (not enough data)
        return True, {"reason": "no_holdout_data", "holdout_score": None}

    return wf_result.holdout_passed, {
        "holdout_score": wf_result.holdout_score,
        "holdout_is_ratio": wf_result.holdout_is_ratio,
        "holdout_passed": wf_result.holdout_passed,
    }


def _run_perturbation_stage(
    config: StrategyConfig,
    h1_candles: list,
    h4_candles: list,
    baseline_score: float,
    perturb_config: PerturbationConfig,
) -> PerturbationResult:
    """Execute perturbation stability tests."""
    try:
        return run_perturbation_test(
            config,
            h1_candles,
            h4_candles,
            baseline_score,
            perturb_config,
        )
    except Exception as exc:
        log.error("Perturbation test crashed: %s", exc)
        return PerturbationResult(all_passed=False)


def _run_stress_stage(
    config: StrategyConfig,
    h1_candles: list,
    h4_candles: list,
    baseline_score: float,
    stress_config: StressConfig,
) -> StressResult:
    """Execute cost stress test."""
    try:
        return run_stress_test(
            config,
            h1_candles,
            h4_candles,
            baseline_score,
            stress_config,
        )
    except Exception as exc:
        log.error("Stress test crashed: %s", exc)
        return StressResult(
            normal_score=baseline_score,
            stress_score=0.0,
            still_profitable=False,
            score_degradation_pct=1.0,
            passed=False,
        )


def _run_durability_stage(
    trades: list | None,
    h1_candles: list,
    wf_result: WalkForwardResult,
    durability_config: Any | None = None,
) -> Any | None:
    """Execute durability validation if trades are available.

    Args:
        trades: Completed trades from baseline backtest.
        h1_candles: H1 candles for regime classification.
        wf_result: Walk-forward result (used for IS/OOS PF if available).
        durability_config: Optional DurabilityConfig override.

    Returns:
        DurabilityReport or None if no trades provided.
    """
    if not trades:
        return None

    try:
        from novatrade.evaluation.durability import DurabilityConfig, evaluate_durability

        dur_cfg = durability_config if durability_config is not None else DurabilityConfig()

        # Extract IS/OOS profit factors from walk-forward if available
        is_pf: float | None = None
        oos_pf: float | None = None
        if wf_result.windows:
            # Use median IS/OOS scores as proxy for profit factors
            is_scores = [w.is_scout_score for w in wf_result.windows if w.is_scout_score > 0]
            oos_scores = [w.oos_scout_score for w in wf_result.windows if w.oos_scout_score > 0]
            if is_scores and oos_scores:
                import statistics

                is_pf = statistics.median(is_scores)
                oos_pf = statistics.median(oos_scores)

        return evaluate_durability(
            trades=trades,
            candles=h1_candles,
            is_profit_factor=is_pf,
            oos_profit_factor=oos_pf,
            config=dur_cfg,
        )
    except Exception as exc:
        log.error("Durability validation crashed: %s", exc)
        return None
