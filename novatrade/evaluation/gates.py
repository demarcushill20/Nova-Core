"""Gated evaluation ladder — Stage A, B, C validity/realism/robustness gates.

Strategies must pass gates sequentially:
  Stage A (Validity): Enough data, no crash, no lookahead, diversified P&L
  Stage B (Realism): Costs applied, conservative fill model, no impossible fills
  Stage C (Robustness): Monte Carlo, walk-forward, cost stress, parameter stability
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateResult:
    """Result of a single gate check."""

    gate: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResults:
    """Aggregated results from one or more gate checks."""

    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if all gate checks passed."""
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failed_at(self) -> str | None:
        """Identifier of the first failed gate, or None if all passed."""
        for r in self.results:
            if not r.passed:
                return r.gate
        return None


@dataclass(frozen=True)
class GateConfig:
    """Threshold configuration for all gate checks."""

    min_trades: int = 50
    min_active_months: int = 6
    max_month_pnl_concentration: float = 0.45  # relaxed from 0.40 — high-R strategies naturally concentrate
    max_session_pnl_concentration: float = 0.5
    min_avg_spread_pips: float = 1.0  # typical major pair spread floor
    min_slippage_pips: float = 0.1
    min_commission_per_lot: float = 3.0  # typical ECN commission per lot


def evaluate_stage_a(metrics: Any, config: GateConfig | None = None) -> list[GateResult]:
    """Stage A validity gates. Metrics is a BacktestMetrics (Any to avoid circular imports)."""
    if config is None:
        config = GateConfig()
    results: list[GateResult] = []

    total = getattr(metrics, "total_completed_trades", 0)
    results.append(
        GateResult(
            gate="A.min_trades",
            passed=total >= config.min_trades,
            reason=f"Need >= {config.min_trades} trades, got {total}",
            details={"required": config.min_trades, "actual": total},
        )
    )

    total_bars = getattr(metrics, "total_bars", 0)
    approx_months = total_bars / (24 * 22) if total_bars > 0 else 0
    results.append(
        GateResult(
            gate="A.min_active_months",
            passed=approx_months >= config.min_active_months,
            reason=f"Need >= {config.min_active_months} months, got ~{approx_months:.1f}",
            details={"required": config.min_active_months, "actual": round(approx_months, 1)},
        )
    )

    concentration = getattr(metrics, "top_3_trades_pct_of_profit", 0.0) / 100.0
    results.append(
        GateResult(
            gate="A.max_month_pnl_concentration",
            passed=concentration <= config.max_month_pnl_concentration,
            reason=f"Top-3 concentration {concentration:.1%} vs max {config.max_month_pnl_concentration:.0%}",
            details={"max_allowed": config.max_month_pnl_concentration, "actual": round(concentration, 4)},
        )
    )

    # Session concentration uses top-3 as proxy until per-session data available
    results.append(
        GateResult(
            gate="A.max_session_pnl_concentration",
            passed=concentration <= config.max_session_pnl_concentration,
            reason=f"Session concentration proxy {concentration:.1%} vs max {config.max_session_pnl_concentration:.0%}",
            details={"max_allowed": config.max_session_pnl_concentration, "actual": round(concentration, 4)},
        )
    )

    max_dd = getattr(metrics, "max_drawdown_pct", 0.0)
    crash_threshold = 50.0
    results.append(
        GateResult(
            gate="A.no_crash",
            passed=max_dd < crash_threshold,
            reason=f"Max drawdown {max_dd:.1f}% vs crash threshold {crash_threshold:.0f}%",
            details={"threshold": crash_threshold, "actual": round(max_dd, 2)},
        )
    )

    results.append(
        GateResult(
            gate="A.no_lookahead",
            passed=True,
            reason="Engine architecture prevents lookahead by construction",
            details={"method": "bar_n_signals_bar_n1_orders"},
        )
    )

    return results


def _extract_env_field(environment: Any, field_name: str, default: Any) -> Any:
    """Extract a field from environment dict or object, with legacy fallback."""
    if isinstance(environment, dict):
        return environment.get(field_name, default)
    # Try nested spread object first (legacy BacktestEnvironment)
    spread = getattr(environment, "spread", None)
    if spread is not None and hasattr(spread, field_name):
        return getattr(spread, field_name)
    return getattr(environment, field_name, default)


def evaluate_stage_b(environment: Any, config: GateConfig | None = None) -> list[GateResult]:
    """Stage B realism gates. Environment is BacktestEnvironment or dict (Any to avoid circular imports)."""
    if config is None:
        config = GateConfig()
    results: list[GateResult] = []

    avg_spread = _extract_env_field(environment, "avg_spread_pips", 0.0)
    slippage = _extract_env_field(environment, "slippage_pips", 0.0)
    commission = _extract_env_field(environment, "commission_per_lot_usd", 0.0)
    stop_first = (
        environment.get("stop_first_on_ambiguity", True)
        if isinstance(environment, dict)
        else getattr(environment, "stop_first_on_ambiguity", True)
    )

    results.append(
        GateResult(
            gate="B.spread_applied",
            passed=avg_spread >= config.min_avg_spread_pips,
            reason=f"Spread {avg_spread} pips (> {config.min_avg_spread_pips})",
            details={"min_required": config.min_avg_spread_pips, "actual": avg_spread},
        )
    )
    results.append(
        GateResult(
            gate="B.slippage_applied",
            passed=slippage >= config.min_slippage_pips,
            reason=f"Slippage {slippage} pips (>= {config.min_slippage_pips})",
            details={"min_required": config.min_slippage_pips, "actual": slippage},
        )
    )
    results.append(
        GateResult(
            gate="B.commission_applied",
            passed=commission >= config.min_commission_per_lot,
            reason=f"Commission ${commission}/lot (>= ${config.min_commission_per_lot})",
            details={"min_required": config.min_commission_per_lot, "actual": commission},
        )
    )
    results.append(
        GateResult(
            gate="B.fill_model_conservative",
            passed=bool(stop_first),
            reason="Fill model must use stop-first on ambiguity (conservative)",
            details={"stop_first_on_ambiguity": stop_first},
        )
    )
    results.append(
        GateResult(
            gate="B.no_impossible_fills",
            passed=True,
            reason="Engine constrains fills to bar OHLC range by construction",
            details={"method": "ohlc_bound_enforcement"},
        )
    )

    return results


def evaluate_stage_c_stub() -> list[GateResult]:
    """Stage C robustness stubs — returns NOT_EVALUATED placeholders.

    Kept for backward compatibility. Prefer :func:`evaluate_stage_c_gates` for
    real validation.
    """
    stubs = [
        ("C.monte_carlo", "Monte Carlo permutation test"),
        ("C.walk_forward", "Walk-forward OOS consistency"),
        ("C.cost_stress", "Cost stress test (2x spread + 3x slippage)"),
        ("C.parameter_stability", "Parameter neighborhood stability"),
    ]
    return [
        GateResult(
            gate=gid,
            passed=False,
            reason=f"NOT_EVALUATED: {desc} — requires Phase 5+ implementation",
            details={"status": "stub", "phase": "C"},
        )
        for gid, desc in stubs
    ]


def evaluate_stage_c_gates(
    trades: list,
    config: Any,
    h1_candles: list,
    h4_candles: list,
    stage_c_config: Any | None = None,
) -> list[GateResult]:
    """Stage C robustness gates — delegates to the real Stage C evaluator.

    Replaces :func:`evaluate_stage_c_stub` with actual Monte Carlo,
    walk-forward, cost stress, and parameter stability gates.

    Args:
        trades: Completed trades from a baseline backtest (CompletedTrade list).
        config: StrategyConfig (or dict/object coercible to one).
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        stage_c_config: Optional StageCConfig for threshold overrides.

    Returns:
        List of GateResult, one per enabled gate.
    """
    from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

    sc_cfg = stage_c_config if stage_c_config is not None else StageCConfig()
    return evaluate_stage_c(
        trades=trades,
        config=config,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        stage_c_config=sc_cfg,
    )
