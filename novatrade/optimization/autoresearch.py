"""Karpathy-style autoresearch loop for strategy discovery.

Evolutionary algorithm that mutates both continuous parameters AND discrete
features (filters, session modes, trail modes) to find robust profitable
strategy variants.  Runs backtests on full historical data and uses
tournament selection + elitism to converge.

Usage:
    result = run_autoresearch(h1_candles, h4_candles, config=AutoResearchConfig())
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field, replace

import numpy as np

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT, BacktestEnvironment
from novatrade.backtest.metrics import EVAL_WINDOWS, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.autoresearch")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Session filter options (None = 24/5 trading)
SESSION_OPTIONS = [None, "london", "newyork", "london_ny_overlap"]

# Discrete feature axes
TRAIL_MODES = ["atr", "ema"]  # trail_ema_period=0 → ATR, >0 → EMA

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
class AutoResearchConfig:
    """Tuning knobs for the autoresearch loop."""

    population_size: int = 60
    generations: int = 50
    elite_count: int = 5
    tournament_size: int = 3
    mutation_rate: float = 0.3  # probability of mutating each param
    feature_flip_rate: float = 0.15  # probability of toggling a feature
    crossover_rate: float = 0.5  # probability of crossover vs pure mutation
    min_trades: int = 50  # minimum trades for a viable strategy
    min_profit_factor: float = 1.05  # minimum PF to be considered "good"
    target_scout_score: float = 0.25  # stop early if we hit this
    seed: int = 42
    verbose: bool = True


@dataclass
class Organism:
    """A single strategy variant in the population."""

    params: dict  # continuous parameter values
    features: dict  # discrete feature toggles
    scout_score: float = 0.0
    profit_factor: float = 0.0
    trade_count: int = 0
    net_pips: float = 0.0
    max_dd_pct: float = 0.0
    generation: int = 0
    parent_id: str = ""
    organism_id: str = ""

    @property
    def viable(self) -> bool:
        return self.trade_count >= 50 and self.profit_factor > 1.0


@dataclass
class AutoResearchResult:
    """Outcome of an autoresearch run."""

    best: Organism | None = None
    viable_count: int = 0
    generations_run: int = 0
    total_backtests: int = 0
    elapsed_seconds: float = 0.0
    history: list[dict] = field(default_factory=list)  # per-generation stats
    top_10: list[Organism] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Genome operations
# ---------------------------------------------------------------------------

BOUNDS = StrategyConfig.PARAMETER_BOUNDS


def _random_organism(rng: np.random.Generator, gen: int = 0, bounds: dict | None = None) -> Organism:
    """Create a fully random organism."""
    b_map = bounds or BOUNDS
    params = {}
    for p in StrategyConfig.OPTIMIZABLE_PARAMS:
        b = b_map[p]
        val = rng.uniform(b.min_val, b.max_val)
        params[p] = round(val) if p in INT_PARAMS else float(val)

    features = {
        "use_ema_stack_filter": bool(rng.choice([True, False])),
        "ema_confirm_bars": int(rng.choice([0, 1, 2, 3, 4, 5])),
        "session_filter": rng.choice(SESSION_OPTIONS),
        "trail_mode": rng.choice(TRAIL_MODES),
        "use_breakeven": bool(rng.choice([True, False])),
        "use_volatility_filter": bool(rng.choice([True, False])),
        "use_circuit_breaker": bool(rng.choice([True, False])),
        "use_trail_delay": bool(rng.choice([True, False])),
    }

    # If trail_mode is "atr", set trail_ema_period=0
    if features["trail_mode"] == "atr":
        params["trail_ema_period"] = 0

    # Apply v4 feature defaults
    if not features["use_breakeven"]:
        params["breakeven_r"] = 0.0
    if not features["use_trail_delay"]:
        params["trail_delay_bars"] = 0
    if not features["use_circuit_breaker"]:
        params["max_consecutive_losses"] = 0

    return Organism(
        params=params,
        features=features,
        generation=gen,
        organism_id=f"g{gen}_{rng.integers(0, 99999):05d}",
    )


def _mutate(
    org: Organism,
    rng: np.random.Generator,
    cfg: AutoResearchConfig,
    gen: int,
    bounds: dict | None = None,
) -> Organism:
    """Mutate an organism — perturb params + maybe flip features."""
    b_map = bounds or BOUNDS
    params = copy.deepcopy(org.params)
    features = copy.deepcopy(org.features)

    # Mutate continuous params
    for p in StrategyConfig.OPTIMIZABLE_PARAMS:
        if rng.random() < cfg.mutation_rate:
            b = b_map[p]
            # Gaussian perturbation (10% of range)
            spread = (b.max_val - b.min_val) * 0.10
            val = params[p] + rng.normal(0, spread)
            val = np.clip(val, b.min_val, b.max_val)
            params[p] = round(float(val)) if p in INT_PARAMS else float(val)

    # Flip features
    if rng.random() < cfg.feature_flip_rate:
        features["use_ema_stack_filter"] = not features["use_ema_stack_filter"]

    if rng.random() < cfg.feature_flip_rate:
        features["ema_confirm_bars"] = int(rng.choice([0, 1, 2, 3, 4, 5]))

    if rng.random() < cfg.feature_flip_rate:
        features["session_filter"] = rng.choice(SESSION_OPTIONS)

    if rng.random() < cfg.feature_flip_rate:
        features["trail_mode"] = rng.choice(TRAIL_MODES)

    # Flip v4 features
    if rng.random() < cfg.feature_flip_rate:
        features["use_breakeven"] = not features.get("use_breakeven", False)
    if rng.random() < cfg.feature_flip_rate:
        features["use_volatility_filter"] = not features.get("use_volatility_filter", False)
    if rng.random() < cfg.feature_flip_rate:
        features["use_circuit_breaker"] = not features.get("use_circuit_breaker", False)
    if rng.random() < cfg.feature_flip_rate:
        features["use_trail_delay"] = not features.get("use_trail_delay", False)

    # Enforce trail mode consistency
    if features["trail_mode"] == "atr":
        params["trail_ema_period"] = 0
    elif params.get("trail_ema_period", 0) == 0:
        params["trail_ema_period"] = int(rng.integers(10, 60))

    # Enforce v4 feature consistency
    if not features.get("use_breakeven", False):
        params["breakeven_r"] = 0.0
    if not features.get("use_trail_delay", False):
        params["trail_delay_bars"] = 0
    if not features.get("use_circuit_breaker", False):
        params["max_consecutive_losses"] = 0

    return Organism(
        params=params,
        features=features,
        generation=gen,
        parent_id=org.organism_id,
        organism_id=f"g{gen}_{rng.integers(0, 99999):05d}",
    )


def _crossover(a: Organism, b: Organism, rng: np.random.Generator, gen: int) -> Organism:
    """Uniform crossover — randomly pick each gene from parent a or b."""
    params = {}
    for p in StrategyConfig.OPTIMIZABLE_PARAMS:
        val = a.params[p] if rng.random() < 0.5 else b.params[p]
        params[p] = round(val) if p in INT_PARAMS else float(val)

    features = {}
    for k in a.features:
        features[k] = a.features[k] if rng.random() < 0.5 else b.features[k]

    # Enforce trail mode consistency
    if features.get("trail_mode") == "atr":
        params["trail_ema_period"] = 0
    elif params.get("trail_ema_period", 0) == 0:
        params["trail_ema_period"] = int(rng.integers(10, 60))

    # Enforce v4 feature consistency
    if not features.get("use_breakeven", False):
        params["breakeven_r"] = 0.0
    if not features.get("use_trail_delay", False):
        params["trail_delay_bars"] = 0
    if not features.get("use_circuit_breaker", False):
        params["max_consecutive_losses"] = 0

    return Organism(
        params=params,
        features=features,
        generation=gen,
        parent_id=f"{a.organism_id}x{b.organism_id}",
        organism_id=f"g{gen}_{rng.integers(0, 99999):05d}",
    )


def _tournament_select(pop: list[Organism], rng: np.random.Generator, k: int = 3) -> Organism:
    """Tournament selection — pick k random, return the best."""
    candidates = rng.choice(len(pop), size=min(k, len(pop)), replace=False)
    best = max(candidates, key=lambda i: pop[i].scout_score)
    return pop[best]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluate(
    org: Organism,
    h1: list[Candle],
    h4: list[Candle],
    base_environment: BacktestEnvironment | None = None,
) -> Organism:
    """Run a backtest and score the organism.

    Args:
        org: Organism to evaluate.
        h1: Primary timeframe candles.
        h4: Higher timeframe candles.
        base_environment: Base environment to overlay strategy params onto.
            Defaults to ``DEFAULT_ENVIRONMENT`` (H1/H4).  Pass
            ``BacktestEnvironment.for_m5()`` for M5 autoresearch.
    """
    try:
        # Build StrategyConfig from params + features
        config_kwargs = copy.deepcopy(org.params)
        config_kwargs["use_ema_stack_filter"] = org.features.get("use_ema_stack_filter", False)
        config_kwargs["ema_confirm_bars"] = org.features.get("ema_confirm_bars", 0)
        config_kwargs["session_filter"] = org.features.get("session_filter")
        config_kwargs["use_volatility_filter"] = org.features.get("use_volatility_filter", False)

        config = StrategyConfig.model_validate(config_kwargs)
        base = base_environment if base_environment is not None else DEFAULT_ENVIRONMENT
        env = replace(base, **config.to_environment_kwargs())
        bt = IRBBacktester(env=env)
        result = bt.run(h1, h4)

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

        org.scout_score = compute_scout_score(metrics)
        org.profit_factor = metrics.profit_factor if metrics.profit_factor != float("inf") else 0.0
        org.trade_count = metrics.total_completed_trades
        org.net_pips = metrics.net_result_pips
        org.max_dd_pct = metrics.max_drawdown_pct

    except Exception as exc:
        log.debug("Organism %s crashed: %s", org.organism_id, exc)
        org.scout_score = 0.0

    return org


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_autoresearch(
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    config: AutoResearchConfig | None = None,
    parameter_bounds: dict[str, object] | None = None,
    base_environment: BacktestEnvironment | None = None,
) -> AutoResearchResult:
    """Run the evolutionary autoresearch loop.

    Maintains a population of strategy variants, evolves them through
    mutation/crossover/selection, and tracks the best performers.

    Args:
        h1_candles: Primary timeframe candles.
        h4_candles: Higher timeframe candles.
        config: Autoresearch configuration.
        parameter_bounds: Optional override for parameter bounds dict.
            Pass ``StrategyConfig.M5_PARAMETER_BOUNDS`` for M5 timeframe.
        base_environment: Base environment to overlay strategy params onto.
            Defaults to ``DEFAULT_ENVIRONMENT`` (H1/H4).  Pass
            ``BacktestEnvironment.for_m5()`` for M5 autoresearch.
    """
    cfg = config or AutoResearchConfig()
    rng = np.random.default_rng(cfg.seed)
    t0 = time.monotonic()
    total_evals = 0

    bounds = parameter_bounds or BOUNDS

    # Initialize population
    population = [_random_organism(rng, gen=0, bounds=bounds) for _ in range(cfg.population_size)]

    # Evaluate initial population
    for org in population:
        _evaluate(org, h1_candles, h4_candles, base_environment=base_environment)
        total_evals += 1

    population.sort(key=lambda o: o.scout_score, reverse=True)
    best_ever = copy.deepcopy(population[0])
    history: list[dict] = []

    if cfg.verbose:
        _log_generation(0, population, best_ever)

    # Evolution loop
    for gen in range(1, cfg.generations + 1):
        new_pop: list[Organism] = []

        # Elitism — carry forward top N unchanged
        for org in population[: cfg.elite_count]:
            elite = copy.deepcopy(org)
            elite.generation = gen
            new_pop.append(elite)

        # Fill rest of population
        while len(new_pop) < cfg.population_size:
            if rng.random() < cfg.crossover_rate and len(population) >= 2:
                # Crossover
                p1 = _tournament_select(population, rng, cfg.tournament_size)
                p2 = _tournament_select(population, rng, cfg.tournament_size)
                child = _crossover(p1, p2, rng, gen)
                # Also mutate the child
                child = _mutate(child, rng, cfg, gen, bounds=bounds)
            else:
                # Pure mutation from tournament winner
                parent = _tournament_select(population, rng, cfg.tournament_size)
                child = _mutate(parent, rng, cfg, gen, bounds=bounds)

            new_pop.append(child)

        # Inject a few random organisms for diversity (10%)
        n_random = max(1, cfg.population_size // 10)
        for i in range(n_random):
            idx = cfg.population_size - 1 - i
            if idx >= cfg.elite_count:
                new_pop[idx] = _random_organism(rng, gen, bounds=bounds)

        # Evaluate new organisms (skip elites — already scored)
        for org in new_pop[cfg.elite_count :]:
            _evaluate(org, h1_candles, h4_candles, base_environment=base_environment)
            total_evals += 1

        new_pop.sort(key=lambda o: o.scout_score, reverse=True)
        population = new_pop

        # Track best ever
        if population[0].scout_score > best_ever.scout_score:
            best_ever = copy.deepcopy(population[0])

        # Generation stats
        viable = [o for o in population if o.viable]
        gen_stats = {
            "generation": gen,
            "best_score": population[0].scout_score,
            "best_pf": population[0].profit_factor,
            "best_trades": population[0].trade_count,
            "best_pips": population[0].net_pips,
            "median_score": float(np.median([o.scout_score for o in population])),
            "viable_count": len(viable),
            "best_ever_score": best_ever.scout_score,
        }
        history.append(gen_stats)

        if cfg.verbose:
            _log_generation(gen, population, best_ever)

        # Early stopping
        if best_ever.scout_score >= cfg.target_scout_score and best_ever.viable:
            if cfg.verbose:
                log.info(
                    "TARGET REACHED at generation %d! Score=%.4f PF=%.2f",
                    gen,
                    best_ever.scout_score,
                    best_ever.profit_factor,
                )
            break

    elapsed = time.monotonic() - t0

    # Collect top 10 unique
    seen = set()
    top_10 = []
    for o in population:
        key = f"{o.scout_score:.4f}_{o.profit_factor:.2f}_{o.trade_count}"
        if key not in seen:
            seen.add(key)
            top_10.append(o)
        if len(top_10) >= 10:
            break

    viable_total = sum(1 for o in population if o.viable)

    return AutoResearchResult(
        best=best_ever,
        viable_count=viable_total,
        generations_run=len(history),
        total_backtests=total_evals,
        elapsed_seconds=round(elapsed, 1),
        history=history,
        top_10=top_10,
    )


def _log_generation(gen: int, pop: list[Organism], best_ever: Organism) -> None:
    """Print a one-line generation summary."""
    best = pop[0]
    viable = sum(1 for o in pop if o.viable)
    median = float(np.median([o.scout_score for o in pop]))
    features = ""
    if best.features:
        f = best.features
        features = (
            f" stack={'Y' if f.get('use_ema_stack_filter') else 'N'}"
            f" confirm={f.get('ema_confirm_bars', 0)}"
            f" trail={f.get('trail_mode', '?')}"
            f" session={f.get('session_filter', 'all') or 'all'}"
            f" BE={'Y' if f.get('use_breakeven') else 'N'}"
            f" vol={'Y' if f.get('use_volatility_filter') else 'N'}"
            f" delay={'Y' if f.get('use_trail_delay') else 'N'}"
        )

    print(
        f"Gen {gen:>3}: best={best.scout_score:.4f} PF={best.profit_factor:.2f} "
        f"trades={best.trade_count:>3} pips={best.net_pips:>+7.0f} "
        f"DD={best.max_dd_pct:.1f}% viable={viable:>2} med={median:.4f}"
        f"{features}"
        f"{'  ★ NEW BEST' if best.scout_score >= best_ever.scout_score and gen > 0 else ''}"
    )
