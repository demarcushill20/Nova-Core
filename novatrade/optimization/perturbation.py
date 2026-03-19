"""Parameter perturbation stability testing.

For each optimisable parameter, bumps it by +/- delta percent and re-runs
the backtest.  A stable strategy should not exhibit large fitness cliffs
when parameters shift slightly — if it does, the optimum is fragile and
likely overfit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.backtest.metrics import EVAL_WINDOWS, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.perturbation")

# ---------------------------------------------------------------------------
# Config and result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerturbationConfig:
    """Knobs for perturbation stability testing."""

    deltas: list[float] = field(default_factory=lambda: [0.05, 0.10])
    max_fitness_drop: float = 0.25  # reject if any bump > 25% drop
    params_to_test: list[str] | None = None  # None = all OPTIMIZABLE_PARAMS


@dataclass
class PerturbResult:
    """Outcome of a single parameter perturbation."""

    param_name: str
    delta: float
    direction: str  # "+" or "-"
    original_value: float
    perturbed_value: float
    original_score: float
    perturbed_score: float
    fitness_drop_pct: float
    passed: bool


@dataclass
class PerturbationResult:
    """Aggregated perturbation stability outcome."""

    results: list[PerturbResult] = field(default_factory=list)
    all_passed: bool = True
    worst_drop: float = 0.0
    cliff_detected: bool = False  # any single perturbation > 50% drop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLIFF_THRESHOLD = 0.50  # 50% drop = cliff


def _run_backtest_score(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
) -> float:
    """Run a single backtest and return the scout score."""
    env_kwargs = config.to_environment_kwargs()
    try:
        environment = replace(DEFAULT_ENVIRONMENT, **env_kwargs)
    except TypeError:
        return 0.0

    bt = IRBBacktester(env=environment)
    result = bt.run(h1_candles, h4_candles)

    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    metrics = compute_metrics(
        trades=result.trades,
        pending_orders=result.pending_orders,
        signals=result.signals,
        filter_rejections=result.filter_rejections,
        window=EVAL_WINDOWS[window_idx],
        total_bars=result.total_bars,
        initial_equity=environment.initial_equity,
    )
    return compute_scout_score(metrics)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_perturbation_test(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    baseline_score: float,
    perturb_config: PerturbationConfig | None = None,
) -> PerturbationResult:
    """Run perturbation stability tests on *config*.

    For every optimisable parameter, each delta, and each direction (+/-),
    creates a perturbed config, runs a backtest, and checks whether the
    fitness drop exceeds the threshold.

    Args:
        config: Strategy configuration under test.
        h1_candles: H1 candle series.
        h4_candles: H4 candle series.
        baseline_score: Scout score of the unperturbed config.
        perturb_config: Perturbation parameters (defaults to
            ``PerturbationConfig()``).

    Returns:
        ``PerturbationResult`` with per-bump details and aggregate verdict.
    """
    pc = perturb_config or PerturbationConfig()
    params = pc.params_to_test or list(StrategyConfig.OPTIMIZABLE_PARAMS)
    bounds = StrategyConfig.PARAMETER_BOUNDS

    outcome = PerturbationResult()

    for param in params:
        if param not in bounds:
            continue

        original_value = float(getattr(config, param))
        pb = bounds[param]

        for delta in pc.deltas:
            for direction in ("+", "-"):
                shift = original_value * delta
                if direction == "+":
                    raw = original_value + shift
                else:
                    raw = original_value - shift

                perturbed_value = _clamp(raw, pb.min_val, pb.max_val)

                # Skip if clamping made no change
                if perturbed_value == original_value:
                    continue

                # Integer params (periods, bar counts) need rounding
                if isinstance(getattr(config, param), int):
                    perturbed_value = float(round(perturbed_value))

                # Build perturbed config
                try:
                    new_config = config.model_copy(
                        update={param: type(getattr(config, param))(perturbed_value)},
                    )
                except Exception:
                    log.warning("Could not perturb %s to %s", param, perturbed_value)
                    continue

                perturbed_score = _run_backtest_score(new_config, h1_candles, h4_candles)

                if baseline_score > 0:
                    drop = (baseline_score - perturbed_score) / baseline_score
                else:
                    drop = 0.0 if perturbed_score >= 0 else 1.0

                drop_pct = max(drop, 0.0)  # negative drop = improvement, not penalised
                passed = drop_pct <= pc.max_fitness_drop

                pr = PerturbResult(
                    param_name=param,
                    delta=delta,
                    direction=direction,
                    original_value=original_value,
                    perturbed_value=perturbed_value,
                    original_score=baseline_score,
                    perturbed_score=perturbed_score,
                    fitness_drop_pct=round(drop_pct, 4),
                    passed=passed,
                )
                outcome.results.append(pr)

                if not passed:
                    outcome.all_passed = False
                if drop_pct > outcome.worst_drop:
                    outcome.worst_drop = round(drop_pct, 4)
                if drop_pct > _CLIFF_THRESHOLD:
                    outcome.cliff_detected = True

                log.info(
                    "Perturb %s %s%.0f%%: %.4f -> %.4f  score %.4f -> %.4f  drop=%.1f%% %s",
                    param,
                    direction,
                    delta * 100,
                    original_value,
                    perturbed_value,
                    baseline_score,
                    perturbed_score,
                    drop_pct * 100,
                    "PASS" if passed else "FAIL",
                )

    return outcome
