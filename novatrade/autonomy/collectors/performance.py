"""Performance Stability collector — Sharpe, drawdown, win-rate, profit factor."""

from __future__ import annotations

import json
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class PerformanceCollector(BaseCollector):
    """Measures performance stability: Sharpe, drawdown, win rate, PF trend."""

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        equity_data = self._load_equity_history()

        # --- sharpe_ratio_30d ---
        try:
            sharpe_score, sharpe_raw = self._compute_sharpe(equity_data)
            sub_metrics.append(
                SubMetric(
                    name="sharpe_ratio_30d",
                    value=self._safe_score(sharpe_score),
                    raw_value=sharpe_raw,
                    description="30-day Sharpe ratio",
                )
            )
        except Exception as exc:
            self.log.warning("sharpe_ratio_30d failed: %s", exc)
            warnings.append(f"sharpe_ratio_30d failed: {exc}")
            sub_metrics.append(SubMetric(name="sharpe_ratio_30d", value=0.0))

        # --- max_drawdown_30d ---
        try:
            dd_score, dd_raw = self._compute_max_drawdown(equity_data)
            sub_metrics.append(
                SubMetric(
                    name="max_drawdown_30d",
                    value=self._safe_score(dd_score),
                    raw_value=dd_raw,
                    description="30-day max drawdown vs FTMO 5% limit",
                )
            )
        except Exception as exc:
            self.log.warning("max_drawdown_30d failed: %s", exc)
            warnings.append(f"max_drawdown_30d failed: {exc}")
            sub_metrics.append(SubMetric(name="max_drawdown_30d", value=0.0))

        # --- win_rate_stability ---
        try:
            wr_score, wr_raw = self._compute_win_rate_stability(equity_data)
            sub_metrics.append(
                SubMetric(
                    name="win_rate_stability",
                    value=self._safe_score(wr_score),
                    raw_value=wr_raw,
                    description="Win rate variance across rolling windows",
                )
            )
        except Exception as exc:
            self.log.warning("win_rate_stability failed: %s", exc)
            warnings.append(f"win_rate_stability failed: {exc}")
            sub_metrics.append(SubMetric(name="win_rate_stability", value=0.0))

        # --- profit_factor_trend ---
        try:
            pf_score, pf_raw = self._compute_profit_factor_trend(equity_data)
            sub_metrics.append(
                SubMetric(
                    name="profit_factor_trend",
                    value=self._safe_score(pf_score),
                    raw_value=pf_raw,
                    description="Profit factor direction",
                )
            )
        except Exception as exc:
            self.log.warning("profit_factor_trend failed: %s", exc)
            warnings.append(f"profit_factor_trend failed: {exc}")
            sub_metrics.append(SubMetric(name="profit_factor_trend", value=0.0))

        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        return DimensionScore(
            name="Performance Stability",
            score=round(avg, 1),
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _load_equity_history(self) -> list[dict]:
        """Load equity history from STATE/novatrade/equity_history.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "equity_history.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _compute_sharpe(self, equity_data: list[dict]) -> tuple[float, float]:
        """Compute Sharpe ratio from equity history."""
        if len(equity_data) < 2:
            return 50.0, -1.0  # placeholder: no data

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        if len(values) < 2:
            return 50.0, -1.0

        # Daily returns
        returns = []
        for i in range(1, len(values)):
            if values[i - 1] != 0:
                returns.append((values[i] - values[i - 1]) / values[i - 1])

        if not returns:
            return 50.0, -1.0

        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std_r = variance**0.5

        if std_r == 0:
            return 50.0, 0.0

        # Annualized Sharpe (assuming daily data, 252 trading days)
        sharpe = (mean_r / std_r) * (252**0.5)

        if sharpe > 1.0:
            score = 100.0
        elif sharpe > 0.5:
            score = 70.0
        elif sharpe > 0:
            score = 50.0
        else:
            score = 20.0

        return score, round(sharpe, 3)

    def _compute_max_drawdown(self, equity_data: list[dict]) -> tuple[float, float]:
        """Compute max drawdown as percentage."""
        if len(equity_data) < 2:
            return 50.0, -1.0  # placeholder: no data

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        if not values or max(values) == 0:
            return 50.0, -1.0

        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd

        dd_pct = max_dd * 100.0

        # Score based on FTMO 5% limit
        if dd_pct < 2:
            score = 100.0
        elif dd_pct < 4:
            score = 70.0
        elif dd_pct < 5:
            score = 40.0
        else:
            score = 0.0

        return score, round(dd_pct, 2)

    def _compute_win_rate_stability(self, equity_data: list[dict]) -> tuple[float, float]:
        """Compute win rate variance across rolling windows."""
        if len(equity_data) < 2:
            return 50.0, -1.0  # placeholder

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        returns = []
        for i in range(1, len(values)):
            returns.append(values[i] - values[i - 1])

        if len(returns) < 4:
            return 50.0, -1.0

        # Split into windows and compute win rate per window
        window_size = max(2, len(returns) // 4)
        win_rates = []
        for start in range(0, len(returns), window_size):
            window = returns[start : start + window_size]
            if not window:
                continue
            wins = sum(1 for r in window if r > 0)
            win_rates.append(wins / len(window))

        if len(win_rates) < 2:
            return 50.0, -1.0

        mean_wr = sum(win_rates) / len(win_rates)
        variance = sum((w - mean_wr) ** 2 for w in win_rates) / len(win_rates)

        # Low variance = high stability
        if variance < 0.01:
            score = 100.0
        elif variance < 0.05:
            score = 70.0
        elif variance < 0.1:
            score = 50.0
        else:
            score = 30.0

        return score, round(variance, 4)

    def _compute_profit_factor_trend(self, equity_data: list[dict]) -> tuple[float, float]:
        """Is profit factor improving, stable, or declining?"""
        if len(equity_data) < 4:
            return 50.0, -1.0  # placeholder

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        returns = []
        for i in range(1, len(values)):
            returns.append(values[i] - values[i - 1])

        if len(returns) < 4:
            return 50.0, -1.0

        # Split into two halves and compute profit factor for each
        mid = len(returns) // 2
        pf_first = self._profit_factor(returns[:mid])
        pf_second = self._profit_factor(returns[mid:])

        if pf_first is None or pf_second is None:
            return 50.0, -1.0

        if pf_second > pf_first * 1.1:
            score = 80.0  # improving
        elif pf_second < pf_first * 0.9:
            score = 30.0  # declining
        else:
            score = 60.0  # stable

        return score, round(pf_second, 3)

    @staticmethod
    def _profit_factor(returns: list[float]) -> float | None:
        """Gross profit / gross loss."""
        gross_profit = sum(r for r in returns if r > 0)
        gross_loss = abs(sum(r for r in returns if r < 0))
        if gross_loss == 0:
            return None
        return gross_profit / gross_loss
