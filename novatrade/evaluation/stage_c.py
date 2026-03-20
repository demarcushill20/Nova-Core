"""Stage C robustness gates — Monte Carlo, walk-forward, cost stress, parameter stability.

Replaces the stubs in gates.evaluate_stage_c_stub() with real implementations.
Each gate returns a GateResult that can be aggregated by the evaluation ladder.

Gates:
  C.monte_carlo       — Permutation test on trade P&L sequence (drawdown distribution)
  C.walk_forward      — OOS/IS ratio from rolling walk-forward validation
  C.cost_stress       — Backtest under inflated transaction costs
  C.parameter_stability — Backtest under perturbed strategy parameters
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, replace
from typing import Any

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT, BacktestEnvironment, SpreadAssumptions
from novatrade.backtest.metrics import (
    EVAL_WINDOWS,
    CompletedTrade,
    compute_metrics,
)
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.evaluation.gates import GateResult
from novatrade.models import Candle
from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

log = logging.getLogger("novatrade.evaluation.stage_c")


# ---------------------------------------------------------------------------
# P5.1: Monte Carlo Simulation Gate
# ---------------------------------------------------------------------------


def monte_carlo_gate(
    trades: list[CompletedTrade],
    n_simulations: int = 1000,
    max_dd_percentile: float = 95.0,
    max_dd_threshold: float = 15.0,
    initial_equity: float = 10000.0,
) -> GateResult:
    """Monte Carlo permutation test on trade P&L sequence.

    Shuffles the trade P&L ordering *n_simulations* times and computes the
    equity-curve max drawdown for each permutation.  The 95th-percentile
    drawdown must stay below *max_dd_threshold* % for the gate to pass.

    This answers: "If these trades had arrived in a different order, would
    the worst-case drawdown still be acceptable?"

    Args:
        trades: Completed trades from a backtest run.
        n_simulations: Number of random permutations to run.
        max_dd_percentile: Percentile of the drawdown distribution to check.
        max_dd_threshold: Maximum acceptable drawdown % at the given percentile.
        initial_equity: Starting equity for equity curve computation.

    Returns:
        GateResult with gate="C.monte_carlo".
    """
    if not trades:
        return GateResult(
            gate="C.monte_carlo",
            passed=False,
            reason="No trades to simulate",
            details={"n_trades": 0},
        )

    pnl_sequence = [t.pnl_usd for t in trades]

    # If all trades have zero USD P&L, fall back to pips (synthetic data)
    if all(p == 0.0 for p in pnl_sequence):
        pnl_sequence = [t.pnl_pips for t in trades]

    drawdowns: list[float] = []
    for _ in range(n_simulations):
        shuffled = pnl_sequence[:]
        random.shuffle(shuffled)
        dd_pct = _max_drawdown_pct(shuffled, initial_equity)
        drawdowns.append(dd_pct)

    drawdowns.sort()

    # Compute the requested percentile
    idx = min(int(len(drawdowns) * max_dd_percentile / 100.0), len(drawdowns) - 1)
    dd_at_percentile = drawdowns[idx]
    dd_median = drawdowns[len(drawdowns) // 2]
    dd_max = drawdowns[-1]

    passed = dd_at_percentile <= max_dd_threshold

    return GateResult(
        gate="C.monte_carlo",
        passed=passed,
        reason=(f"MC {max_dd_percentile:.0f}th-pctl DD = {dd_at_percentile:.2f}% (threshold {max_dd_threshold:.1f}%)"),
        details={
            f"dd_{int(max_dd_percentile)}th": round(dd_at_percentile, 4),
            "dd_median": round(dd_median, 4),
            "dd_max": round(dd_max, 4),
            "n_simulations": n_simulations,
            "n_trades": len(trades),
            "initial_equity": initial_equity,
            "threshold": max_dd_threshold,
        },
    )


def _max_drawdown_pct(pnl_sequence: list[float], initial_equity: float) -> float:
    """Compute max drawdown % from a P&L sequence given initial equity."""
    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0

    for pnl in pnl_sequence:
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    return max_dd


# ---------------------------------------------------------------------------
# P5.2: Walk-Forward Validation Gate
# ---------------------------------------------------------------------------


def walk_forward_gate(
    config: Any,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    min_oos_ratio: float = 0.6,
    min_holdout_ratio: float = 0.7,
) -> GateResult:
    """Walk-forward validation gate using existing walk-forward engine.

    Delegates to ``run_walk_forward()`` and checks that the OOS/IS ratio and
    holdout score meet minimum thresholds.

    Args:
        config: StrategyConfig instance.
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        min_oos_ratio: Minimum OOS profit factor as fraction of IS.
        min_holdout_ratio: Minimum holdout score as fraction of IS.

    Returns:
        GateResult with gate="C.walk_forward".
    """
    # Coerce to StrategyConfig if needed
    if not isinstance(config, StrategyConfig):
        try:
            config = StrategyConfig.model_validate(config)
        except Exception as exc:
            return GateResult(
                gate="C.walk_forward",
                passed=False,
                reason=f"Invalid config: {exc}",
                details={"error": str(exc)},
            )

    wf_config = WalkForwardConfig(
        min_oos_is_ratio=min_oos_ratio,
        min_holdout_ratio=min_holdout_ratio,
    )

    try:
        wf_result = run_walk_forward(config, h1_candles, h4_candles, wf_config=wf_config)
    except Exception as exc:
        log.warning("Walk-forward execution failed: %s", exc)
        return GateResult(
            gate="C.walk_forward",
            passed=False,
            reason=f"Walk-forward execution failed: {exc}",
            details={"error": str(exc)},
        )

    if not wf_result.windows:
        return GateResult(
            gate="C.walk_forward",
            passed=False,
            reason="Insufficient data for walk-forward (no windows generated)",
            details={"n_windows": 0},
        )

    # Check OOS/IS ratio
    oos_ratio_ok = wf_result.median_oos_is_ratio >= min_oos_ratio

    # Check holdout
    holdout_ok = wf_result.holdout_passed is True or wf_result.holdout_passed is None
    holdout_score = wf_result.holdout_score if wf_result.holdout_score is not None else 0.0

    passed = oos_ratio_ok and holdout_ok

    return GateResult(
        gate="C.walk_forward",
        passed=passed,
        reason=(
            f"WF median OOS/IS ratio = {wf_result.median_oos_is_ratio:.4f} "
            f"(min {min_oos_ratio:.2f}), "
            f"holdout={'PASS' if holdout_ok else 'FAIL'}"
        ),
        details={
            "median_oos_score": round(wf_result.median_oos_score, 4),
            "median_is_score": round(wf_result.median_is_score, 4),
            "median_oos_is_ratio": round(wf_result.median_oos_is_ratio, 4),
            "holdout_score": round(holdout_score, 4),
            "holdout_passed": wf_result.holdout_passed,
            "n_windows": len(wf_result.windows),
            "all_windows_passed": wf_result.all_windows_passed,
            "overall_passed": wf_result.overall_passed,
        },
    )


# ---------------------------------------------------------------------------
# P5.3: Cost Stress Test Gate
# ---------------------------------------------------------------------------


def cost_stress_gate(
    config: Any,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    spread_multiplier: float = 2.0,
    slippage_multiplier: float = 3.0,
    commission_multiplier: float = 2.0,
) -> GateResult:
    """Cost stress test — re-run backtest with inflated transaction costs.

    Multiplies spread, slippage, and commission by the given factors and
    checks that the strategy remains profitable under stress.

    Args:
        config: StrategyConfig instance.
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        spread_multiplier: Factor to multiply average spread by.
        slippage_multiplier: Factor to multiply slippage by.
        commission_multiplier: Factor to multiply commission by.

    Returns:
        GateResult with gate="C.cost_stress".
    """
    if not isinstance(config, StrategyConfig):
        try:
            config = StrategyConfig.model_validate(config)
        except Exception as exc:
            return GateResult(
                gate="C.cost_stress",
                passed=False,
                reason=f"Invalid config: {exc}",
                details={"error": str(exc)},
            )

    # Build baseline environment
    env_kwargs = config.to_environment_kwargs()
    try:
        base_env = replace(DEFAULT_ENVIRONMENT, **env_kwargs)
    except TypeError as exc:
        return GateResult(
            gate="C.cost_stress",
            passed=False,
            reason=f"Environment construction failed: {exc}",
            details={"error": str(exc)},
        )

    # Run baseline backtest
    original_pf, original_net_pips = _run_backtest_and_score(base_env, h1_candles, h4_candles)

    # Build stressed environment with inflated costs
    base_spread = base_env.spread
    stressed_spread = SpreadAssumptions(
        fixed_spread_pips=(
            base_spread.fixed_spread_pips * spread_multiplier if base_spread.fixed_spread_pips is not None else None
        ),
        avg_spread_pips=base_spread.avg_spread_pips * spread_multiplier,
        slippage_pips=base_spread.slippage_pips * slippage_multiplier,
        commission_per_lot_usd=base_spread.commission_per_lot_usd * commission_multiplier,
        description=(
            f"Stress test: spread x{spread_multiplier}, "
            f"slippage x{slippage_multiplier}, "
            f"commission x{commission_multiplier}"
        ),
    )
    stressed_env = replace(base_env, spread=stressed_spread)

    # Run stressed backtest
    stress_pf, stress_net_pips = _run_backtest_and_score(stressed_env, h1_candles, h4_candles)

    # Pass if still profitable under stress
    passed = stress_pf > 1.0 or stress_net_pips > 0.0

    return GateResult(
        gate="C.cost_stress",
        passed=passed,
        reason=(
            f"Stress PF = {stress_pf:.4f}, net pips = {stress_net_pips:.2f} "
            f"(spread x{spread_multiplier}, slip x{slippage_multiplier}, comm x{commission_multiplier})"
        ),
        details={
            "stress_pf": round(stress_pf, 4),
            "stress_net_pips": round(stress_net_pips, 2),
            "original_pf": round(original_pf, 4),
            "original_net_pips": round(original_net_pips, 2),
            "spread_multiplier": spread_multiplier,
            "slippage_multiplier": slippage_multiplier,
            "commission_multiplier": commission_multiplier,
        },
    )


def _run_backtest_and_score(
    env: BacktestEnvironment,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
) -> tuple[float, float]:
    """Run a backtest and return (profit_factor, net_result_pips).

    Returns (0.0, 0.0) on failure or if there are no trades.
    """
    try:
        bt = IRBBacktester(env=env)
        result = bt.run(h1_candles, h4_candles)
    except Exception as exc:
        log.warning("Backtest execution failed in cost stress: %s", exc)
        return 0.0, 0.0

    if not result.trades:
        return 0.0, 0.0

    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    metrics = compute_metrics(
        trades=result.trades,
        pending_orders=result.pending_orders,
        signals=result.signals,
        filter_rejections=result.filter_rejections,
        window=EVAL_WINDOWS[window_idx],
        total_bars=result.total_bars,
        initial_equity=env.initial_equity,
    )
    return metrics.profit_factor, metrics.net_result_pips


# ---------------------------------------------------------------------------
# P5.4: Parameter Stability Gate
# ---------------------------------------------------------------------------


def parameter_stability_gate(
    config: Any,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    n_perturbations: int = 10,
    perturbation_pct: float = 0.10,
) -> GateResult:
    """Parameter neighbourhood stability test.

    Perturbs each optimisable parameter by +/- *perturbation_pct* and runs
    a backtest for each perturbation.  The gate passes if >= 70% of
    perturbations still produce a positive scout score.

    This detects "cliff" parameters — if a 10% shift kills the strategy,
    the optimised parameters are fragile and likely overfit.

    Args:
        config: StrategyConfig instance.
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        n_perturbations: Max number of perturbations to sample.
        perturbation_pct: Fractional perturbation (0.10 = +/-10%).

    Returns:
        GateResult with gate="C.parameter_stability".
    """
    if not isinstance(config, StrategyConfig):
        try:
            config = StrategyConfig.model_validate(config)
        except Exception as exc:
            return GateResult(
                gate="C.parameter_stability",
                passed=False,
                reason=f"Invalid config: {exc}",
                details={"error": str(exc)},
            )

    # Generate perturbations: for each optimisable param, create +pct and -pct variants
    perturbations: list[StrategyConfig] = []
    base_values = config.to_environment_kwargs()

    for param_name in StrategyConfig.OPTIMIZABLE_PARAMS:
        base_val = base_values.get(param_name)
        if base_val is None:
            continue

        bounds = StrategyConfig.PARAMETER_BOUNDS.get(param_name)
        is_int = isinstance(base_val, int)

        for direction in (-1, 1):
            delta = base_val * perturbation_pct * direction
            new_val = base_val + delta

            # Clamp to bounds
            if bounds is not None:
                new_val = max(bounds.min_val, min(new_val, bounds.max_val))

            if is_int:
                new_val = int(round(new_val))  # noqa: RUF046

            # Skip if perturbation is identical to base (e.g., at boundary)
            if new_val == base_val:
                continue

            try:
                perturbed = config.model_copy(update={param_name: new_val})
                perturbations.append(perturbed)
            except Exception:  # noqa: S112
                continue  # skip invalid perturbations

    # Cap at n_perturbations (randomly sample if we have more)
    if len(perturbations) > n_perturbations:
        perturbations = random.sample(perturbations, n_perturbations)

    if not perturbations:
        return GateResult(
            gate="C.parameter_stability",
            passed=False,
            reason="No valid perturbations could be generated",
            details={"n_tested": 0},
        )

    # Run backtests on all perturbations
    n_stable = 0
    n_tested = 0
    per_param_results: list[dict[str, Any]] = []

    for perturbed_config in perturbations:
        env_kwargs = perturbed_config.to_environment_kwargs()
        try:
            env = replace(DEFAULT_ENVIRONMENT, **env_kwargs)
        except TypeError:
            continue

        try:
            bt = IRBBacktester(env=env)
            result = bt.run(h1_candles, h4_candles)
        except Exception as exc:
            log.debug("Perturbation backtest failed: %s", exc)
            n_tested += 1
            continue

        if not result.trades:
            n_tested += 1
            continue

        window_idx = min(2, len(EVAL_WINDOWS) - 1)
        metrics = compute_metrics(
            trades=result.trades,
            pending_orders=result.pending_orders,
            signals=result.signals,
            filter_rejections=result.filter_rejections,
            window=EVAL_WINDOWS[window_idx],
            total_bars=result.total_bars,
            initial_equity=env.initial_equity,
        )
        score = compute_scout_score(metrics)
        n_tested += 1
        if score > 0.0:
            n_stable += 1

        per_param_results.append(
            {
                "config_hash": perturbed_config.content_hash(),
                "scout_score": round(score, 4),
                "stable": score > 0.0,
            }
        )

    stability_ratio = n_stable / n_tested if n_tested > 0 else 0.0
    passed = stability_ratio >= 0.70

    return GateResult(
        gate="C.parameter_stability",
        passed=passed,
        reason=(f"Stability ratio = {stability_ratio:.1%} ({n_stable}/{n_tested} perturbations stable, threshold 70%)"),
        details={
            "stability_ratio": round(stability_ratio, 4),
            "n_stable": n_stable,
            "n_tested": n_tested,
            "perturbation_pct": perturbation_pct,
            "perturbation_results": per_param_results,
        },
    )


# ---------------------------------------------------------------------------
# P5.5: Stage C Evaluator (integration)
# ---------------------------------------------------------------------------


@dataclass
class StageCConfig:
    """Configuration for which Stage C gates to run and their thresholds."""

    run_monte_carlo: bool = True
    run_walk_forward: bool = True
    run_cost_stress: bool = True
    run_param_stability: bool = True

    # Monte Carlo settings
    mc_simulations: int = 1000
    mc_dd_threshold: float = 15.0
    mc_dd_percentile: float = 95.0
    mc_initial_equity: float = 10000.0

    # Walk-forward settings
    wf_min_oos_ratio: float = 0.6
    wf_min_holdout_ratio: float = 0.7

    # Cost stress settings
    stress_spread_mult: float = 2.0
    stress_slippage_mult: float = 3.0
    stress_commission_mult: float = 2.0

    # Parameter stability settings
    stability_n_perturbations: int = 10
    stability_perturbation_pct: float = 0.10


def evaluate_stage_c(
    trades: list[CompletedTrade],
    config: Any,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    stage_c_config: StageCConfig | None = None,
) -> list[GateResult]:
    """Run all enabled Stage C robustness gates and return results.

    Gates are run in order: Monte Carlo, walk-forward, cost stress,
    parameter stability.  Each gate runs independently — a failure in
    one does not prevent the others from executing.

    Args:
        trades: Completed trades from a baseline backtest run.
        config: StrategyConfig (or dict/object coercible to one).
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        stage_c_config: Optional configuration overrides.

    Returns:
        List of GateResult, one per enabled gate.
    """
    cfg = stage_c_config or StageCConfig()
    results: list[GateResult] = []

    if cfg.run_monte_carlo:
        log.info("Running Stage C: Monte Carlo gate (%d simulations)", cfg.mc_simulations)
        results.append(
            monte_carlo_gate(
                trades=trades,
                n_simulations=cfg.mc_simulations,
                max_dd_percentile=cfg.mc_dd_percentile,
                max_dd_threshold=cfg.mc_dd_threshold,
                initial_equity=cfg.mc_initial_equity,
            )
        )

    if cfg.run_walk_forward:
        log.info("Running Stage C: Walk-forward gate")
        results.append(
            walk_forward_gate(
                config=config,
                h1_candles=h1_candles,
                h4_candles=h4_candles,
                min_oos_ratio=cfg.wf_min_oos_ratio,
                min_holdout_ratio=cfg.wf_min_holdout_ratio,
            )
        )

    if cfg.run_cost_stress:
        log.info("Running Stage C: Cost stress gate")
        results.append(
            cost_stress_gate(
                config=config,
                h1_candles=h1_candles,
                h4_candles=h4_candles,
                spread_multiplier=cfg.stress_spread_mult,
                slippage_multiplier=cfg.stress_slippage_mult,
                commission_multiplier=cfg.stress_commission_mult,
            )
        )

    if cfg.run_param_stability:
        log.info("Running Stage C: Parameter stability gate")
        results.append(
            parameter_stability_gate(
                config=config,
                h1_candles=h1_candles,
                h4_candles=h4_candles,
                n_perturbations=cfg.stability_n_perturbations,
                perturbation_pct=cfg.stability_perturbation_pct,
            )
        )

    return results
