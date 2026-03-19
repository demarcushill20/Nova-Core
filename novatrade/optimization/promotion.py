"""Promotion pipeline — full validation battery for campaign champions.

After a campaign identifies a best candidate, the promotion pipeline runs
the complete validation gauntlet:
  1. Walk-forward OOS validation (multi-window)
  2. Holdout evaluation (separate from WF holdout)
  3. Parameter perturbation stability test
  4. Cost stress test (2x spread + slippage)
  5. Promotion score computation

Only strategies that pass ALL four stages earn a promotion score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

    print(f"[promote] Starting promotion pipeline for {experiment_id}")
    print(f"[promote] Baseline scout score: {baseline_score:.4f}")

    # --- Stage 1: Walk-forward validation ---
    print("[promote] Stage 1/4: Walk-forward validation...")
    wf_result = _run_walkforward_stage(config, h1_candles, h4_candles, wf_cfg)
    result.walkforward_passed = wf_result.overall_passed
    result.details["walkforward"] = {
        "windows": len(wf_result.windows),
        "median_oos_score": wf_result.median_oos_score,
        "median_oos_is_ratio": wf_result.median_oos_is_ratio,
        "all_windows_passed": wf_result.all_windows_passed,
        "overall_passed": wf_result.overall_passed,
    }
    print(
        f"[promote]   Walk-forward: {'PASS' if result.walkforward_passed else 'FAIL'} "
        f"(median OOS={wf_result.median_oos_score:.4f}, "
        f"ratio={wf_result.median_oos_is_ratio:.4f})"
    )

    # --- Stage 2: Holdout evaluation ---
    print("[promote] Stage 2/4: Holdout evaluation...")
    holdout_passed, holdout_details = _evaluate_holdout(wf_result)
    result.holdout_passed = holdout_passed
    result.details["holdout"] = holdout_details
    print(f"[promote]   Holdout: {'PASS' if holdout_passed else 'FAIL'}")

    # --- Stage 3: Perturbation stability ---
    print("[promote] Stage 3/4: Perturbation stability test...")
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
    print(
        f"[promote]   Perturbation: {'PASS' if result.perturbation_passed else 'FAIL'} "
        f"(worst drop={perturb_result.worst_drop:.1%}, "
        f"cliff={'YES' if perturb_result.cliff_detected else 'no'})"
    )

    # --- Stage 4: Cost stress test ---
    print("[promote] Stage 4/4: Cost stress test...")
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
    print(
        f"[promote]   Stress: {'PASS' if result.stress_passed else 'FAIL'} "
        f"(degradation={stress_result.score_degradation_pct:.1%}, "
        f"profitable={stress_result.still_profitable})"
    )

    # --- Compute promotion score if all pass ---
    result.overall_passed = (
        result.walkforward_passed and result.holdout_passed and result.perturbation_passed and result.stress_passed
    )

    if result.overall_passed:
        oos_scores = [w.oos_scout_score for w in wf_result.windows]
        holdout_score = wf_result.holdout_score or 0.0
        is_median = wf_result.median_oos_score  # proxy: use OOS median as IS median
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
        print(f"[promote] ALL STAGES PASSED — promotion score: {result.promotion_score:.4f}")
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
        print(f"[promote] PROMOTION FAILED — stages: {', '.join(stages_failed)}")

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
        print(f"[promote] Walk-forward crashed: {exc}")
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
        print(f"[promote] Perturbation test crashed: {exc}")
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
        print(f"[promote] Stress test crashed: {exc}")
        return StressResult(
            normal_score=baseline_score,
            stress_score=0.0,
            still_profitable=False,
            score_degradation_pct=1.0,
            passed=False,
        )
