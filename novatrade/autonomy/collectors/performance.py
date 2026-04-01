"""Performance Stability collector — Sharpe, drawdown, win-rate, profit factor."""

from __future__ import annotations

import json
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class PerformanceCollector(BaseCollector):
    """Measures performance stability: Sharpe, drawdown, win rate, PF trend."""

    # Sentinel returned by sub-metric helpers when data is insufficient.
    # Use neutral 50.0 instead of pessimistic 30.0 — confidence handles uncertainty.
    _NO_DATA_SCORE = 50.0
    _NO_DATA_RAW = -1.0

    # Minimum trades for full confidence
    _MIN_TRADES_FULL = 30
    _MIN_TRADES_PARTIAL = 10

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
        data_points = len(equity_data)
        if not has_data:
            warnings.append(
                f"No equity data available — scores are neutral placeholders "
                f"({self._NO_DATA_SCORE:.0f}/100), not indicators of poor performance"
            )
        elif no_data_count > 0:
            warnings.append(
                f"{no_data_count}/{len(_metrics)} metrics lack sufficient data — partial placeholder scores in effect"
            )

        # Add insufficient_data sub-metric showing data availability
        sub_metrics.append(
            SubMetric(
                name="insufficient_data",
                value=min(100.0, (data_points / self._MIN_TRADES_FULL) * 100.0),
                raw_value=float(data_points),
                description=f"Data points: {data_points}/{self._MIN_TRADES_FULL} needed for full confidence",
            )
        )

        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        # Set confidence based on data availability
        if data_points >= self._MIN_TRADES_FULL:
            confidence = 1.0
        elif data_points >= self._MIN_TRADES_PARTIAL:
            confidence = 0.5
        elif data_points >= 2:
            confidence = 0.3
        else:
            confidence = 0.1

        return DimensionScore(
            name="Performance Stability",
            score=round(avg, 1),
            confidence=confidence,
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _load_equity_history(self) -> list[dict]:
        """Load equity history from STATE/novatrade/equity_history.json.

        Handles both list format ``[{...}, ...]`` and dict format
        ``{"snapshots": [...], ...}``.  Deduplicates entries with identical
        timestamps (keeps the last occurrence per normalized timestamp) and
        sorts chronologically.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "equity_history.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                entries = data.get("snapshots", [])
            else:
                return []
            return self._deduplicate_and_sort(entries)
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _normalize_ts(ts: str) -> str:
        """Normalize ISO timestamp for dedup: Z → +00:00, strip microseconds."""
        return ts.replace("Z", "+00:00").split(".")[0]

    # Maximum single-step equity change to consider valid (3%).
    # Entries exceeding this are likely stale adapter returns or data corruption.
    _MAX_STEP_CHANGE = 0.03

    @classmethod
    def _deduplicate_and_sort(cls, entries: list[dict]) -> list[dict]:
        """Deduplicate by normalized timestamp, keeping last per timestamp, then sort."""
        by_ts: dict[str, dict] = {}
        for entry in entries:
            raw_ts = entry.get("timestamp", "")
            key = cls._normalize_ts(raw_ts) if raw_ts else f"_no_ts_{id(entry)}"
            by_ts[key] = entry
        result = list(by_ts.values())
        result.sort(key=lambda e: cls._normalize_ts(e.get("timestamp", "")))
        return cls._filter_anomalies(result)

    @classmethod
    def _filter_anomalies(cls, sorted_entries: list[dict]) -> list[dict]:
        """Remove entries that create single-step equity changes beyond threshold.

        Defence-in-depth: even if a bad value slips past the writer guard,
        the collector will not let it distort drawdown/Sharpe calculations.
        """
        if len(sorted_entries) < 2:
            return sorted_entries
        clean: list[dict] = [sorted_entries[0]]
        for entry in sorted_entries[1:]:
            prev_eq = clean[-1].get("equity", clean[-1].get("value", 0))
            cur_eq = entry.get("equity", entry.get("value", 0))
            if prev_eq > 0 and abs(cur_eq - prev_eq) / prev_eq > cls._MAX_STEP_CHANGE:
                continue  # skip anomalous entry
            clean.append(entry)
        return clean

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
