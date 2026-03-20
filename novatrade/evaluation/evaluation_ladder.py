"""Gated evaluation ladder — replaces single-scalar optimization.

Instead of optimising a single scout_score directly, strategies pass
through a multi-stage ladder:

  Stage A (Validity): Binary pass/fail gates.
    - Minimum trade count (>=50)
    - No catastrophic drawdown (<50%)
    - Positive expectancy after costs
    - Diversified P&L (no top-3 concentration >40%)
    - No lookahead (by engine construction)

  Stage B (Multi-Metric Ranking): Survivors get ranked on a dashboard
    using multiple metrics — NOT optimised against a single number.
    Metrics: Sharpe, Sortino, profit factor, max DD, recovery factor,
    trade frequency, regime stability (when available).

  Stage C (Promotion Ladder): Champions pass full validation:
    Walk-forward → holdout → perturbation → stress → promotion score.

The ranking score is used for SORTING survivors, not as an optimisation
target.  This prevents the optimiser from gaming a single metric.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Ladder stages
# ---------------------------------------------------------------------------


class LadderStage(Enum):
    """Where a strategy sits on the evaluation ladder."""

    REJECTED = "rejected"  # failed Stage A
    GATED_A = "gated_a"  # failed specific Stage A gate
    GATED_B = "gated_b"  # passed A but failed B thresholds
    RANKED = "ranked"  # passed A+B, on the dashboard
    VALIDATED = "validated"  # passed Stage C walk-forward
    PROMOTED = "promoted"  # passed full promotion pipeline
    LIVE_CANDIDATE = "live"  # approved for paper/live trading


# ---------------------------------------------------------------------------
# Multi-metric dashboard row
# ---------------------------------------------------------------------------


@dataclass
class DashboardRow:
    """One strategy's multi-metric ranking row.

    These metrics are displayed side-by-side for human review.
    The ranking_score is a convenience for sorting but is NOT the
    optimisation target.
    """

    experiment_id: str = ""

    # Stage A outcome
    stage_a_passed: bool = False
    stage_a_failures: list[str] = field(default_factory=list)

    # Multi-metric columns (Stage B)
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    recovery_factor: float = 0.0
    trade_count: int = 0
    trades_per_month: float = 0.0
    calmar_ratio: float = 0.0
    expectancy_r: float = 0.0
    win_rate: float = 0.0
    avg_win_loss_ratio: float = 0.0

    # Regime-aware metrics (populated when regime labels available)
    regime_labels: dict[str, float] = field(default_factory=dict)
    # e.g. {"trending": 0.72, "ranging": 0.31, "volatile": 0.15}

    # Ranking score — for SORTING only, not optimisation
    ranking_score: float = 0.0

    # Stage C outcome (populated after promotion pipeline)
    stage_c_passed: bool | None = None
    stage_c_gates: list[Any] = field(default_factory=list)  # list[GateResult]
    promotion_score: float | None = None

    # Durability outcome (populated after durability validation)
    durability_grade: str | None = None  # A/B/C/D/F
    durability_passed: bool | None = None

    ladder_stage: LadderStage = LadderStage.REJECTED

    def to_dict(self) -> dict[str, Any]:
        """Serialise to dict for JSON/TSV export."""
        return {
            "experiment_id": self.experiment_id,
            "ladder_stage": self.ladder_stage.value,
            "stage_a_passed": self.stage_a_passed,
            "stage_a_failures": self.stage_a_failures,
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "recovery_factor": round(self.recovery_factor, 4),
            "trade_count": self.trade_count,
            "trades_per_month": round(self.trades_per_month, 2),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "win_rate": round(self.win_rate, 4),
            "avg_win_loss_ratio": round(self.avg_win_loss_ratio, 4),
            "ranking_score": round(self.ranking_score, 4),
            "regime_labels": self.regime_labels,
            "stage_c_passed": self.stage_c_passed,
            "stage_c_gates": [
                {"gate": g.gate, "passed": g.passed, "reason": g.reason}
                for g in self.stage_c_gates
                if hasattr(g, "gate")
            ],
            "promotion_score": (round(self.promotion_score, 4) if self.promotion_score is not None else None),
            "durability_grade": self.durability_grade,
            "durability_passed": self.durability_passed,
        }


# ---------------------------------------------------------------------------
# Multi-metric computation
# ---------------------------------------------------------------------------


def compute_dashboard_metrics(
    metrics: Any,
    experiment_id: str = "",
    gate_a_results: list | None = None,
) -> DashboardRow:
    """Compute a full DashboardRow from backtest metrics.

    Args:
        metrics: BacktestMetrics instance (Any to avoid circular imports).
        experiment_id: Experiment identifier.
        gate_a_results: Pre-computed Stage A gate results (list of GateResult).
            If None, Stage A is not evaluated here.

    Returns:
        DashboardRow with all available metrics populated.
    """
    row = DashboardRow(experiment_id=experiment_id)

    # --- Stage A ---
    if gate_a_results is not None:
        row.stage_a_passed = all(g.passed for g in gate_a_results)
        row.stage_a_failures = [g.gate for g in gate_a_results if not g.passed]
    else:
        row.stage_a_passed = True  # assume passed if not checked

    # --- Multi-metric extraction ---
    row.profit_factor = _safe_float(metrics, "profit_factor", 0.0)
    row.max_drawdown_pct = _safe_float(metrics, "max_drawdown_pct", 0.0)
    row.trade_count = getattr(metrics, "total_completed_trades", 0)
    row.expectancy_r = _safe_float(metrics, "expectancy_r", 0.0)
    row.win_rate = _safe_float(metrics, "win_rate", 0.0)

    # Derived metrics
    total_bars = getattr(metrics, "total_bars", 0)
    net_pips = _safe_float(metrics, "net_result_pips", 0.0)

    # Trades per month (approx: 1 month ≈ 22 trading days × 24 bars/day)
    bars_per_month = 22 * 24
    months = total_bars / bars_per_month if total_bars > 0 else 1.0
    row.trades_per_month = row.trade_count / max(months, 0.01)

    # Annualised return for ratio computation
    trading_days = total_bars / 24.0 if total_bars > 0 else 1.0
    annual_return = (net_pips / max(trading_days, 1.0)) * 252.0

    # Calmar ratio
    if row.max_drawdown_pct > 0:
        row.calmar_ratio = annual_return / row.max_drawdown_pct
    else:
        row.calmar_ratio = 5.0 if annual_return > 0 else 0.0

    # Sharpe and Sortino ratios — prefer per-trade-return-based values from
    # BacktestMetrics (computed in metrics.py).  Fall back to a simple proxy
    # when the metrics object does not provide them (e.g. mock objects in tests).
    metrics_sharpe = _safe_float(metrics, "sharpe_ratio", 0.0)
    metrics_sortino = _safe_float(metrics, "sortino_ratio", 0.0)

    if metrics_sharpe != 0.0 or metrics_sortino != 0.0:
        row.sharpe_ratio = metrics_sharpe
        row.sortino_ratio = metrics_sortino
    else:
        # Fallback proxy when per-trade data is unavailable
        if row.max_drawdown_pct > 0 and annual_return > 0:
            row.sharpe_ratio = annual_return / (row.max_drawdown_pct * 10.0)
        else:
            row.sharpe_ratio = 0.0
        row.sortino_ratio = row.sharpe_ratio * 1.2 if row.sharpe_ratio > 0 else 0.0

    # Recovery factor: net_result_usd / max_drawdown_usd (consistent USD units)
    net_usd = _safe_float(metrics, "net_result_usd", 0.0)
    max_dd_usd = _safe_float(metrics, "max_drawdown_usd", 0.0)
    if max_dd_usd > 0:
        row.recovery_factor = net_usd / max_dd_usd
    else:
        row.recovery_factor = 5.0 if net_usd > 0 else 0.0

    # Avg win/loss ratio
    avg_win = _safe_float(metrics, "average_winner_pips", 0.0)
    avg_loss = abs(_safe_float(metrics, "average_loser_pips", 0.0))
    if avg_loss > 0:
        row.avg_win_loss_ratio = avg_win / avg_loss
    else:
        row.avg_win_loss_ratio = 3.0 if avg_win > 0 else 0.0

    # --- Ranking score (for sorting only) ---
    row.ranking_score = _compute_ranking_score(row)

    # --- Ladder stage ---
    if not row.stage_a_passed:
        row.ladder_stage = LadderStage.GATED_A
    elif row.ranking_score > 0:
        row.ladder_stage = LadderStage.RANKED
    else:
        row.ladder_stage = LadderStage.GATED_B

    return row


def _safe_float(obj: Any, attr: str, default: float) -> float:
    """Extract a float attribute, handling inf/nan gracefully."""
    val = getattr(obj, attr, default)
    if val is None:
        return default
    val = float(val)
    if val == float("inf") or val == float("-inf") or val != val:
        return default
    return val


def _compute_ranking_score(row: DashboardRow) -> float:
    """Compute a ranking score from multi-metric dashboard data.

    This is a convenience for SORTING — not an optimisation target.
    The operator reviews the full dashboard, not just this number.

    Weighted blend:
      30% profit factor (capped 3.0)
      20% calmar ratio (capped 5.0)
      15% expectancy R (mapped [-1, 2] → [0, 1])
      15% recovery factor (capped 5.0)
      10% win rate
      10% trades per month (capped 20, normalized)
    """
    if row.trade_count < 30:
        return 0.0

    pf_norm = min(row.profit_factor, 3.0) / 3.0
    calmar_norm = min(max(row.calmar_ratio, 0.0), 5.0) / 5.0
    exp_norm = (min(max(row.expectancy_r, -1.0), 2.0) + 1.0) / 3.0
    recovery_norm = min(max(row.recovery_factor, 0.0), 5.0) / 5.0
    wr_norm = min(row.win_rate, 1.0)
    freq_norm = min(row.trades_per_month, 20.0) / 20.0

    raw = (
        0.30 * pf_norm + 0.20 * calmar_norm + 0.15 * exp_norm + 0.15 * recovery_norm + 0.10 * wr_norm + 0.10 * freq_norm
    )

    # Penalties
    penalty = 0.0
    if row.max_drawdown_pct > 10.0:
        penalty += 0.10
    if row.max_drawdown_pct > 20.0:
        penalty += 0.10

    return max(min(raw - penalty, 1.0), 0.0)


# ---------------------------------------------------------------------------
# Dashboard (leaderboard) operations
# ---------------------------------------------------------------------------


@dataclass
class EvaluationDashboard:
    """Multi-metric leaderboard for human review.

    Strategies are ranked by ranking_score for convenience, but the
    operator reviews the full metric columns to make decisions.
    """

    rows: list[DashboardRow] = field(default_factory=list)

    def add(self, row: DashboardRow) -> None:
        """Add a strategy to the dashboard."""
        self.rows.append(row)

    @property
    def ranked(self) -> list[DashboardRow]:
        """Return only RANKED+ strategies, sorted by ranking_score desc."""
        eligible = [
            r
            for r in self.rows
            if r.ladder_stage
            in (
                LadderStage.RANKED,
                LadderStage.VALIDATED,
                LadderStage.PROMOTED,
                LadderStage.LIVE_CANDIDATE,
            )
        ]
        return sorted(eligible, key=lambda r: r.ranking_score, reverse=True)

    @property
    def rejected(self) -> list[DashboardRow]:
        """Return strategies that failed gating."""
        return [
            r for r in self.rows if r.ladder_stage in (LadderStage.REJECTED, LadderStage.GATED_A, LadderStage.GATED_B)
        ]

    def validate_stage_c(
        self,
        experiment_id: str,
        gate_results: list,
    ) -> bool:
        """Apply Stage C gate results to a strategy on the dashboard.

        If all gates pass, the strategy moves to VALIDATED stage.  Gate
        results are stored for diagnostic review regardless of outcome.

        Args:
            experiment_id: Experiment to update.
            gate_results: List of GateResult objects from Stage C evaluation.

        Returns:
            True if the experiment was found and updated, False otherwise.
        """
        for row in self.rows:
            if row.experiment_id == experiment_id:
                row.stage_c_gates = gate_results
                all_passed = bool(gate_results) and all(getattr(g, "passed", False) for g in gate_results)
                row.stage_c_passed = all_passed
                if all_passed and row.ladder_stage == LadderStage.RANKED:
                    row.ladder_stage = LadderStage.VALIDATED
                return True
        return False

    def promote(self, experiment_id: str, promotion_score: float) -> bool:
        """Mark a strategy as promoted (passed Stage C).

        Returns True if found and promoted, False otherwise.
        """
        for row in self.rows:
            if row.experiment_id == experiment_id:
                row.stage_c_passed = True
                row.promotion_score = promotion_score
                row.ladder_stage = LadderStage.PROMOTED
                return True
        return False

    def set_durability(
        self,
        experiment_id: str,
        durability_grade: str,
        durability_passed: bool,
    ) -> bool:
        """Record durability validation results for a strategy.

        Args:
            experiment_id: Experiment to update.
            durability_grade: Composite grade (A/B/C/D/F).
            durability_passed: Whether all durability gates passed.

        Returns:
            True if the experiment was found and updated, False otherwise.
        """
        for row in self.rows:
            if row.experiment_id == experiment_id:
                row.durability_grade = durability_grade
                row.durability_passed = durability_passed
                return True
        return False

    def export_tsv(self) -> str:
        """Export the dashboard as TSV (export format, not canonical store).

        Per doctrine: TSV is an export, not the canonical store.
        The canonical store is the ExperimentDB (SQLite).
        """
        headers = [
            "experiment_id",
            "stage",
            "ranking_score",
            "pf",
            "calmar",
            "sharpe",
            "sortino",
            "max_dd_pct",
            "recovery",
            "exp_r",
            "win_rate",
            "wl_ratio",
            "trades",
            "trades_mo",
            "promo_score",
        ]
        lines = ["\t".join(headers)]

        for row in self.ranked:
            promo = f"{row.promotion_score:.4f}" if row.promotion_score is not None else "—"
            lines.append(
                "\t".join(
                    [
                        row.experiment_id,
                        row.ladder_stage.value,
                        f"{row.ranking_score:.4f}",
                        f"{row.profit_factor:.2f}",
                        f"{row.calmar_ratio:.2f}",
                        f"{row.sharpe_ratio:.4f}",
                        f"{row.sortino_ratio:.4f}",
                        f"{row.max_drawdown_pct:.2f}",
                        f"{row.recovery_factor:.2f}",
                        f"{row.expectancy_r:.4f}",
                        f"{row.win_rate:.2f}",
                        f"{row.avg_win_loss_ratio:.2f}",
                        str(row.trade_count),
                        f"{row.trades_per_month:.1f}",
                        promo,
                    ]
                )
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Success criteria (per doctrine redlines)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuccessCriteria:
    """Revised success criteria per operator redlines.

    A strategy is successful when:
    1. A single run is reproducible from frozen artifacts
    2. Backtest engine completes under target runtime on cached data
    3. Strategy passes all three gating stages (A, B, C)
    4. Promotion pipeline confirms OOS robustness
    """

    reproducible_from_frozen: bool = False
    engine_under_target_runtime: bool = False
    target_runtime_seconds: float = 120.0
    all_gates_passed: bool = False
    promotion_passed: bool = False

    @property
    def met(self) -> bool:
        """True if all success criteria are met."""
        return (
            self.reproducible_from_frozen
            and self.engine_under_target_runtime
            and self.all_gates_passed
            and self.promotion_passed
        )
