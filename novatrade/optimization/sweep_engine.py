"""Parameter sweep engines — random search and Latin hypercube sampling."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

import numpy as np

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.backtest.metrics import EVAL_WINDOWS, EvaluationWindow, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.sweep_engine")

INT_PARAMS = {
    "ema_period",
    "ema_fast_period",
    "ema_slow_period",
    "atr_period",
    "adx_period",
    "trigger_window_bars",
    "time_stop_bars",
    "mtf_lookback",
    "warmup_bars",
    "trail_ema_period",
    "ema_confirm_bars",
    "trail_delay_bars",
    "volatility_atr_ma_period",
    "max_consecutive_losses",
    # v5 params
    "ema_slope_lookback",
    "max_trades_per_day",
    "cooldown_bars",
}


@dataclass(frozen=True)
class SweepConfig:
    """Configuration for a parameter sweep run."""

    n_experiments: int = 100
    method: str = "random"  # "random" | "latin_hypercube"
    seed: int | None = None
    params_to_sweep: list[str] | None = None


@dataclass
class SweepExperiment:
    """Single sweep experiment result."""

    index: int
    config: dict
    scout_score: float = 0.0
    trade_count: int = 0
    profit_factor: float | None = None
    status: str = "ranked"  # "ranked" | "gated" | "crashed"


@dataclass
class SweepResult:
    """Aggregated sweep results."""

    experiments: list[SweepExperiment] = field(default_factory=list)
    best: SweepExperiment | None = None
    total_experiments: int = 0
    ranked_count: int = 0
    gated_count: int = 0
    crashed_count: int = 0
    elapsed_seconds: float = 0.0


def _resolve_params(params: list[str] | None) -> list[str]:
    """Resolve param list — default to all OPTIMIZABLE_PARAMS."""
    if params:
        return [p for p in params if p in StrategyConfig.PARAMETER_BOUNDS]
    return list(StrategyConfig.OPTIMIZABLE_PARAMS)


def _snap_to_type(name: str, value: float) -> int | float:
    """Round integer params to int, keep floats as-is."""
    return round(value) if name in INT_PARAMS else float(value)


def generate_random_configs(
    n: int,
    params: list[str] | None = None,
    seed: int | None = None,
) -> list[dict]:
    """Generate n random parameter configs within PARAMETER_BOUNDS."""
    rng = np.random.default_rng(seed)
    resolved = _resolve_params(params)
    bounds = StrategyConfig.PARAMETER_BOUNDS
    configs: list[dict] = []
    for _ in range(n):
        cfg = {p: _snap_to_type(p, rng.uniform(bounds[p].min_val, bounds[p].max_val)) for p in resolved}
        configs.append(cfg)
    return configs


def generate_latin_hypercube(
    n: int,
    params: list[str] | None = None,
    seed: int | None = None,
) -> list[dict]:
    """Latin hypercube sampling — one sample per stratum, shuffled per dimension."""
    rng = np.random.default_rng(seed)
    resolved = _resolve_params(params)
    bounds = StrategyConfig.PARAMETER_BOUNDS
    d = len(resolved)
    # Build n x d unit-hypercube samples
    samples = np.empty((n, d))
    for j in range(d):
        perm = rng.permutation(n)
        for i in range(n):
            samples[i, j] = rng.uniform(perm[i] / n, (perm[i] + 1) / n)
    # Scale to parameter bounds
    configs: list[dict] = []
    for i in range(n):
        cfg: dict = {}
        for j, p in enumerate(resolved):
            b = bounds[p]
            cfg[p] = _snap_to_type(p, b.min_val + samples[i, j] * (b.max_val - b.min_val))
        configs.append(cfg)
    return configs


def run_sweep(
    base_config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    sweep_config: SweepConfig | None = None,
) -> SweepResult:
    """Execute a full parameter sweep, returning experiments sorted by scout score."""
    sc = sweep_config or SweepConfig()
    t0 = time.monotonic()

    # Generate candidate param dicts
    if sc.method == "latin_hypercube":
        candidates = generate_latin_hypercube(sc.n_experiments, sc.params_to_sweep, sc.seed)
    else:
        candidates = generate_random_configs(sc.n_experiments, sc.params_to_sweep, sc.seed)

    # Evaluation window — broadest available
    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    eval_window = EVAL_WINDOWS[window_idx]

    experiments: list[SweepExperiment] = []

    for idx, param_dict in enumerate(candidates):
        exp = _run_single(base_config, h1_candles, h4_candles, param_dict, idx, eval_window)
        experiments.append(exp)

        if (idx + 1) % 25 == 0:
            log.info("Sweep progress: %d / %d", idx + 1, sc.n_experiments)

    # Sort by scout_score descending
    experiments.sort(key=lambda e: e.scout_score, reverse=True)

    ranked = sum(1 for e in experiments if e.status == "ranked")
    gated = sum(1 for e in experiments if e.status == "gated")
    crashed = sum(1 for e in experiments if e.status == "crashed")
    best = experiments[0] if experiments and experiments[0].status == "ranked" else None

    elapsed = time.monotonic() - t0

    return SweepResult(
        experiments=experiments,
        best=best,
        total_experiments=len(experiments),
        ranked_count=ranked,
        gated_count=gated,
        crashed_count=crashed,
        elapsed_seconds=round(elapsed, 2),
    )


def _run_single(
    base_config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    param_dict: dict,
    index: int,
    eval_window: EvaluationWindow,
) -> SweepExperiment:
    """Run a single experiment, returning a SweepExperiment."""
    try:
        config = base_config.model_copy(update=param_dict)
        env = replace(DEFAULT_ENVIRONMENT, **config.to_environment_kwargs())
        bt = IRBBacktester(env=env)
        result = bt.run(h1_candles, h4_candles)

        metrics = compute_metrics(
            trades=result.trades,
            pending_orders=result.pending_orders,
            signals=result.signals,
            filter_rejections=result.filter_rejections,
            window=eval_window,
            total_bars=result.total_bars,
            initial_equity=env.initial_equity,
        )

        score = compute_scout_score(metrics)
        pf: float | None = metrics.profit_factor
        if pf == float("inf") or pf == float("-inf"):
            pf = None

        status = "ranked" if score > 0 else "gated"
        return SweepExperiment(
            index=index,
            config=param_dict,
            scout_score=score,
            trade_count=metrics.total_completed_trades,
            profit_factor=pf,
            status=status,
        )

    except Exception as exc:
        log.warning("Experiment %d crashed: %s", index, exc)
        return SweepExperiment(
            index=index,
            config=param_dict,
            scout_score=0.0,
            trade_count=0,
            profit_factor=None,
            status="crashed",
        )
