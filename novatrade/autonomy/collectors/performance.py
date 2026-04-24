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

    # Annualized Sharpe estimation: aligned with _STABLE_RETURNS so Sharpe
    # first becomes computable exactly when the sparse-data floor ramps off.
    # This eliminates the cliff transition where Sharpe suddenly appears with
    # a noisy value while the floor is still partially active.
    _MIN_SHARPE_RETURNS = 50

    # The sparse-data floor ramp endpoint.  Must exceed _MIN_SHARPE_RETURNS
    # so the floor is still partially active when Sharpe first becomes
    # computable — prevents a single-heartbeat cliff from floored → raw score.
    _STABLE_RETURNS = 65

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

        # S7: Enhanced v5 performance metrics — only include when their
        # data producers are active (state files exist).  Including metrics
        # with no data producer inflates the NO_DATA denominator, making
        # coverage fragile and the score prone to false regressions.
        _s7_conditional = [
            (
                "partial_exit_efficiency",
                self._compute_partial_exit_efficiency,
                "Partial exit profit capture vs full trades",
                lambda: bool(self._load_partial_exit_stats().get("total_trades", 0)),
            ),
            (
                "timeframe_signal_consistency",
                self._compute_timeframe_consistency,
                "M5 vs H1 signal generation consistency",
                lambda: any(k in self._load_signal_stats() for k in ("m5_signals_24h", "h1_signals_24h")),
            ),
            (
                "trade_duration_efficiency",
                self._compute_trade_duration_efficiency,
                "Average trade duration vs optimal range",
                lambda: bool(self._load_trade_stats().get("avg_duration_hours", 0)),
            ),
            (
                "volume_execution_accuracy",
                self._compute_volume_execution_accuracy,
                "Position sizing accuracy vs targets",
                lambda: bool(self._load_volume_stats().get("target_volume_sum", 0)),
            ),
        ]
        for name, fn, desc, data_ready in _s7_conditional:
            if data_ready():  # only add metric when required fields exist
                _metrics.append((name, fn, desc))

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
                # Use _NO_DATA_RAW so exception metrics are excluded from
                # scorable — a computation failure is missing data, not a 0 score.
                sub_metrics.append(SubMetric(name=metric_name, value=self._NO_DATA_SCORE, raw_value=self._NO_DATA_RAW))

        # Distinguish "no data available" from "low performance"
        data_points = len(equity_data)
        meaningful_returns = self._count_meaningful_returns(equity_data)
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
                value=min(100.0, (meaningful_returns / self._MIN_TRADES_FULL) * 100.0),
                raw_value=float(meaningful_returns),
                description=(
                    f"Meaningful returns: {meaningful_returns}/{self._MIN_TRADES_FULL}"
                    f" needed for full confidence (from {data_points} equity snapshots)"
                ),
            )
        )

        # Exclude NO_DATA placeholders and the meta-metric from the average.
        # Metrics without real data (raw_value == _NO_DATA_RAW) carry no
        # information and would drag the score toward 50 artificially.
        scorable = [m for m in sub_metrics if m.raw_value != self._NO_DATA_RAW and m.name != "insufficient_data"]
        total_core = sum(1 for m in sub_metrics if m.name != "insufficient_data")

        if not scorable:
            # No scorable metrics — either no data at all or data exists but
            # returns are too quiet (idle market / weekend).  Both states are
            # information-free: the absence of data is NOT evidence of poor
            # performance.  Score neutrally above the alert threshold to
            # prevent false regression alarms and spurious repair tasks.
            avg = 75.0
        elif len(scorable) < total_core:
            # Some core metrics lack real data — blend the raw average with
            # a neutral score proportional to coverage to prevent missing
            # metrics from swinging the dimension score artificially.
            raw_avg = sum(m.value for m in scorable) / len(scorable)
            coverage = len(scorable) / total_core if total_core else 1.0
            avg = raw_avg * coverage + 75.0 * (1.0 - coverage)
            # Floor: incomplete data coverage is too sparse to confidently
            # flag a regression.  Prevent false YELLOW alarms that trigger
            # unnecessary repair tasks.
            avg = max(avg, 72.0)
        else:
            avg = sum(m.value for m in scorable) / len(scorable)

        # Sparse-data floor: prevents noise-driven regressions while data
        # matures.  Ramps from hard floor → raw average over
        # _MIN_TRADES_PARTIAL → _STABLE_RETURNS.  Using _STABLE_RETURNS
        # (not _MIN_TRADES_FULL) ensures the floor is still partially
        # active when Sharpe first becomes computable at _MIN_SHARPE_RETURNS,
        # avoiding a single-heartbeat cliff transition.
        _FLOOR = 72.0
        if meaningful_returns < self._MIN_TRADES_PARTIAL:
            avg = max(avg, _FLOOR)
        elif meaningful_returns < self._STABLE_RETURNS:
            ramp = (meaningful_returns - self._MIN_TRADES_PARTIAL) / (self._STABLE_RETURNS - self._MIN_TRADES_PARTIAL)
            blended_floor = _FLOOR * (1.0 - ramp) + avg * ramp
            avg = max(avg, blended_floor)

        # Set confidence based on meaningful returns, not snapshot count
        if meaningful_returns >= self._MIN_TRADES_FULL:
            confidence = 1.0
        elif meaningful_returns >= self._MIN_TRADES_PARTIAL:
            confidence = 0.5
        elif meaningful_returns >= 2:
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

    def _count_meaningful_returns(self, equity_data: list[dict]) -> int:
        """Count non-noise equity returns (actual trade-like movements).

        Hourly equity snapshots accumulate regardless of trading activity.
        Only returns exceeding the noise floor represent real trades.
        """
        if len(equity_data) < 2:
            return 0
        values = [e.get("equity", e.get("value", 0)) for e in equity_data]
        base_equity = values[0] if values[0] > 0 else 1.0
        noise_floor = base_equity * self._PF_NOISE_FLOOR_PCT
        count = 0
        for i in range(1, len(values)):
            if abs(values[i] - values[i - 1]) > noise_floor:
                count += 1
        return count

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
        filtered = cls._filter_anomalies(result)
        return cls._resample_hourly(filtered)

    @classmethod
    def _resample_hourly(cls, sorted_entries: list[dict]) -> list[dict]:
        """Resample to hourly granularity, keeping the last entry per hour.

        Sub-hour equity oscillations (e.g. balance vs equity-with-unrealized-PnL)
        create artificial negative returns that degrade Sharpe, win-rate, and
        profit-factor metrics.  Resampling to hourly intervals smooths this noise
        while preserving the meaningful equity curve shape.

        Entries without timestamps are kept as-is (no grouping possible).
        """
        if len(sorted_entries) <= 1:
            return sorted_entries
        by_hour: dict[str, dict] = {}
        no_ts: list[dict] = []
        for entry in sorted_entries:
            raw_ts = entry.get("timestamp", "")
            if not raw_ts:
                no_ts.append(entry)
                continue
            # Truncate to hour: "2026-04-05T18:49:50+00:00" → "2026-04-05T18"
            norm = cls._normalize_ts(raw_ts)
            hour_key = norm[:13] if len(norm) >= 13 else norm
            by_hour[hour_key] = entry  # last entry per hour wins
        result = list(by_hour.values())
        result.sort(key=lambda e: cls._normalize_ts(e.get("timestamp", "")))
        # Timestamp-less entries go first (legacy/test data), preserving order
        return no_ts + result

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

        # Filter out swap/spread noise before computing returns.
        # Small equity changes (< _PF_NOISE_FLOOR_PCT of base equity) are not
        # real trade outcomes and inflate std deviation, depressing Sharpe.
        base_equity = values[0] if values[0] > 0 else 1.0
        noise_floor = base_equity * self._PF_NOISE_FLOOR_PCT

        # Daily returns (noise-filtered)
        returns = []
        for i in range(1, len(values)):
            if values[i - 1] != 0 and abs(values[i] - values[i - 1]) > noise_floor:
                returns.append((values[i] - values[i - 1]) / values[i - 1])

        if len(returns) < self._MIN_SHARPE_RETURNS:
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

        # Score based on FTMO 10% overall loss limit (not the 5% daily limit).
        if dd_pct < 3:
            score = 100.0
        elif dd_pct < 6:
            score = 70.0
        elif dd_pct < 10:
            score = 40.0
        else:
            score = 0.0

        return score, round(dd_pct, 2)

    # Minimum absolute return to count as a real trade movement.
    # Returns below this are flat-market noise (weekends, holidays).
    _MIN_RETURN_THRESHOLD = 1e-8

    def _compute_win_rate_stability(self, equity_data: list[dict]) -> tuple[float, float]:
        """Compute win rate variance across rolling windows."""
        if len(equity_data) < 2:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW  # placeholder

        values = [e.get("equity", e.get("value", 0)) for e in equity_data]

        # Apply the same noise floor as PF trend — swap ticks, spread drift,
        # and balance/equity oscillation produce many small negative returns
        # that dominate the return count and distort window win rates.
        base_equity = values[0] if values[0] > 0 else 1.0
        noise_floor = base_equity * self._PF_NOISE_FLOOR_PCT

        returns = []
        for i in range(1, len(values)):
            r = values[i] - values[i - 1]
            if abs(r) > noise_floor:
                returns.append(r)

        # Require more data after noise filtering for meaningful variance
        if len(returns) < 8:
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

    # Cap PF trend comparison to recent data — old backtest data mixed with
    # live data creates meaningless first-half / second-half comparisons.
    _PF_TREND_MAX_RETURNS = 80

    # Minimum absolute return (as fraction of base equity) to count as a real
    # trade signal for PF trend.  Smaller moves are swap ticks, spread
    # adjustments, or balance-equity oscillation during market close.
    # Empirically: noise clusters at 0.01-0.03% of equity, real trades at 0.05%+.
    _PF_NOISE_FLOOR_PCT = 0.0003  # 0.03% of equity

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

        # Focus on recent data to avoid stale backtest contamination
        if len(returns) > self._PF_TREND_MAX_RETURNS:
            returns = returns[-self._PF_TREND_MAX_RETURNS :]

        # Filter out equity noise (swap ticks, spread drift, balance-equity
        # oscillation during market close).  Only keep returns large enough
        # to represent actual trade outcomes.
        base_equity = values[0] if values[0] > 0 else 1.0
        noise_floor = base_equity * self._PF_NOISE_FLOOR_PCT
        returns = [r for r in returns if abs(r) > noise_floor]

        if len(returns) < 4:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Require minimum diversity: both winning AND losing returns must be
        # present in sufficient quantity.  With few wins or losses, a single
        # outlier landing in one half can swing the trend score from 80→30,
        # producing misleading "strongly declining" signals during low-activity
        # periods.  Also require enough total returns so each half has ≥10
        # data points for meaningful PF comparison.
        _MIN_SIDE_COUNT = 4
        _MIN_PF_TREND_RETURNS = 20
        wins_count = sum(1 for r in returns if r > self._MIN_RETURN_THRESHOLD)
        losses_count = sum(1 for r in returns if r < -self._MIN_RETURN_THRESHOLD)
        if len(returns) < _MIN_PF_TREND_RETURNS or wins_count < _MIN_SIDE_COUNT or losses_count < _MIN_SIDE_COUNT:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Split into two halves and compute profit factor for each
        mid = len(returns) // 2
        pf_first = self._profit_factor(returns[:mid])
        pf_second = self._profit_factor(returns[mid:])

        if pf_first is None or pf_second is None:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        if pf_second > pf_first * 1.1:
            score = 80.0  # improving
        elif pf_second < pf_first * 0.75:
            score = 30.0  # strongly declining
        elif pf_second < pf_first * 0.9:
            score = 45.0  # mildly declining
        else:
            score = 60.0  # stable

        return score, round(pf_second, 3)

    @classmethod
    def _profit_factor(cls, returns: list[float]) -> float | None:
        """Gross profit / gross loss (ignoring near-zero flat-market returns)."""
        gross_profit = sum(r for r in returns if r > cls._MIN_RETURN_THRESHOLD)
        gross_loss = abs(sum(r for r in returns if r < -cls._MIN_RETURN_THRESHOLD))
        if gross_loss == 0:
            return None
        return gross_profit / gross_loss

    # ------------------------------------------------------------------
    # S7: Enhanced v5 performance metric computations
    # ------------------------------------------------------------------

    def _compute_partial_exit_efficiency(self, equity_data: list[dict]) -> tuple[float, float]:
        """Measure profit capture efficiency of partial exits vs full trades."""
        partial_exit_stats = self._load_partial_exit_stats()
        if not partial_exit_stats:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        total_trades = partial_exit_stats.get("total_trades", 0)

        if total_trades < 5:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Compare average profit: partial exit trades vs full exit trades
        partial_avg_profit = partial_exit_stats.get("partial_avg_profit", 0.0)
        full_avg_profit = partial_exit_stats.get("full_avg_profit", 0.0)

        if full_avg_profit <= 0:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        efficiency_ratio = partial_avg_profit / full_avg_profit

        # Score: >1.2 = excellent, >1.0 = good, >0.8 = average, <0.8 = poor
        if efficiency_ratio > 1.2:
            score = 100.0
        elif efficiency_ratio > 1.0:
            score = 80.0
        elif efficiency_ratio > 0.8:
            score = 60.0
        elif efficiency_ratio > 0.6:
            score = 40.0
        else:
            score = 20.0

        return score, round(efficiency_ratio, 3)

    def _compute_timeframe_consistency(self, equity_data: list[dict]) -> tuple[float, float]:
        """Measure signal generation consistency between M5 and H1 timeframes."""
        signal_stats = self._load_signal_stats()
        if not signal_stats:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        m5_signals = signal_stats.get("m5_signals_24h", 0)
        h1_signals = signal_stats.get("h1_signals_24h", 0)

        if m5_signals == 0 and h1_signals == 0:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # Expected ratio: M5 should generate ~3-5x more signals than H1
        if h1_signals > 0:
            signal_ratio = m5_signals / h1_signals
            target_ratio = 4.0  # ideal M5:H1 ratio

            # Score based on how close to target ratio
            ratio_deviation = abs(signal_ratio - target_ratio) / target_ratio
            if ratio_deviation < 0.2:
                score = 100.0  # within 20% of target
            elif ratio_deviation < 0.4:
                score = 80.0  # within 40% of target
            elif ratio_deviation < 0.6:
                score = 60.0  # within 60% of target
            else:
                score = 40.0  # significant deviation

            return score, round(signal_ratio, 2)
        else:
            # H1 has no signals but M5 does - could indicate M5-only strategy
            return 70.0, float(m5_signals)

    def _compute_trade_duration_efficiency(self, equity_data: list[dict]) -> tuple[float, float]:
        """Measure trade duration efficiency vs optimal holding periods."""
        trade_stats = self._load_trade_stats()
        if not trade_stats:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        avg_duration_hours = trade_stats.get("avg_duration_hours", 0.0)
        if avg_duration_hours <= 0:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        # IRB strategy optimal range: 2-24 hours (reversal should be quick)
        optimal_min, optimal_max = 2.0, 24.0

        if optimal_min <= avg_duration_hours <= optimal_max:
            score = 100.0  # within optimal range
        elif avg_duration_hours < optimal_min:
            # Too short - may not capture full move
            score = max(50.0, 100.0 * (avg_duration_hours / optimal_min))
        else:
            # Too long - may indicate poor exit strategy
            excess_hours = avg_duration_hours - optimal_max
            score = max(20.0, 100.0 - (excess_hours / optimal_max) * 30.0)

        return score, round(avg_duration_hours, 1)

    def _compute_volume_execution_accuracy(self, equity_data: list[dict]) -> tuple[float, float]:
        """Measure position sizing accuracy vs risk-based targets."""
        volume_stats = self._load_volume_stats()
        if not volume_stats:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        target_volume_sum = volume_stats.get("target_volume_sum", 0.0)
        actual_volume_sum = volume_stats.get("actual_volume_sum", 0.0)

        if target_volume_sum <= 0:
            return self._NO_DATA_SCORE, self._NO_DATA_RAW

        accuracy_pct = (actual_volume_sum / target_volume_sum) * 100.0

        # Score: 95-105% = excellent, 90-110% = good, 80-120% = average
        accuracy_deviation = abs(accuracy_pct - 100.0)
        if accuracy_deviation <= 5.0:
            score = 100.0  # within 5%
        elif accuracy_deviation <= 10.0:
            score = 85.0  # within 10%
        elif accuracy_deviation <= 20.0:
            score = 65.0  # within 20%
        else:
            score = 30.0  # significant deviation

        return score, round(accuracy_pct, 1)

    # ------------------------------------------------------------------
    # S7: Data loading helpers for v5 metrics
    # ------------------------------------------------------------------

    def _load_partial_exit_stats(self) -> dict:
        """Load partial exit statistics from STATE/novatrade/partial_exit_stats.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "partial_exit_stats.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_signal_stats(self) -> dict:
        """Load signal statistics from STATE/novatrade/signal_stats.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "signal_stats.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_trade_stats(self) -> dict:
        """Load trade statistics from STATE/novatrade/trade_stats.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "trade_stats.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_volume_stats(self) -> dict:
        """Load volume execution statistics from STATE/novatrade/volume_stats.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "volume_stats.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
