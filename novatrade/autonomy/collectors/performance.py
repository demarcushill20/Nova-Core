"""Performance Stability collector — Sharpe, drawdown, win-rate, profit factor."""

from __future__ import annotations

import json
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class PerformanceCollector(BaseCollector):
    """Measures performance stability: Sharpe, drawdown, win rate, PF trend."""

    # Sentinel returned by sub-metric helpers when data is insufficient.
    _NO_DATA_SCORE = 30.0
    _NO_DATA_RAW = -1.0

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        equity_data = self._load_equity_history()
        has_data = len(equity_data) >= 2

        _metrics = [
            ("sharpe_ratio_30d", self._compute_sharpe, "30-day Sharpe ratio"),
            ("max_drawdown_30d", self._compute_max_drawdown, "30-day max drawdown vs FTMO 5% limit"),
            ("win_rate_stability", self._compute_win_rate_stability, "Win rate variance across rolling windows"),
            ("profit_factor_trend", self._compute_profit_factor_trend, "Profit factor direction"),
        ]

        no_data_count = 0
        for metric_name, compute_fn, description in _metrics:
            try:
                score, raw = compute_fn(equity_data)
                if raw == self._NO_DATA_RAW:
                    no_data_count += 1
                    warnings.append(f"Insufficient equity data for {metric_name}")
                    description = f"[NO DATA] {description}"
                sub_metrics.append(
                    SubMetric(
                        name=metric_name,
                        value=self._safe_score(score),
                        raw_value=raw,
                        description=description,
                    )
                )
            except Exception as exc:
                self.log.warning("%s failed: %s", metric_name, exc)
                warnings.append(f"{metric_name} failed: {exc}")
                sub_metrics.append(SubMetric(name=metric_name, value=0.0))

        # Distinguish "no data available" from "low performance"
        if not has_data:
            warnings.append(
                f"No equity data available — scores are placeholders "
                f"({self._NO_DATA_SCORE:.0f}/100), not indicators of poor performance"
            )
        elif no_data_count > 0:
            warnings.append(
                f"{no_data_count}/{len(_metrics)} metrics lack sufficient data — partial placeholder scores in effect"
            )

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
        """Load equity history from STATE/novatrade/equity_history.json.

        Handles both list format ``[{...}, ...]`` and dict format
        ``{"snapshots": [...], ...}``.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "equity_history.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("snapshots", [])
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _compute_sharpe(self, equity_data: list[dict]) -> tuple[float, float]:
        """Compute Sharpe ratio from equity history."""
        if len(equity_data) < 2:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        if len(values) < 2:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Daily returns
        returns = []
        for i in range(1, len(values)):
            if values[i - 1] != 0:
                returns.append((values[i] - values[i - 1]) / values[i - 1])

        if not returns:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std_r = variance**0.5

        if std_r == 0:
            return 60.0, 0.0  # zero volatility = stable (not a placeholder)

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
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        if not values or max(values) == 0:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

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
            return self._NO_DATA_SCORE, self._NO_DATA_RAW  # placeholder

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        returns = []
        for i in range(1, len(values)):
            returns.append(values[i] - values[i - 1])

        if len(returns) < 4:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

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
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

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
            return self._NO_DATA_SCORE, self._NO_DATA_RAW  # placeholder

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        returns = []
        for i in range(1, len(values)):
            returns.append(values[i] - values[i - 1])

        if len(returns) < 4:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Split into two halves and compute profit factor for each
        mid = len(returns) // 2
        pf_first = self._profit_factor(returns[:mid])
        pf_second = self._profit_factor(returns[mid:])

        if pf_first is None or pf_second is None:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

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
