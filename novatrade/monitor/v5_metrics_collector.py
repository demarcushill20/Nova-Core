"""V5 Metrics Collector for S7 Performance Monitoring Enhancement.

Collects and persists enhanced metrics for v5 features:
- Partial exit statistics and efficiency
- Multi-timeframe signal consistency
- Trade duration and volume execution accuracy
- Enhanced strategy performance indicators

Writes metrics to STATE/novatrade/ files for consumption by performance analyzer
and autonomy collectors.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")


@dataclass
class PartialExitStats:
    """Partial exit performance statistics."""

    total_trades: int = 0
    partial_trades: int = 0
    partial_exits_count: int = 0
    partial_avg_profit: float = 0.0
    full_avg_profit: float = 0.0
    partial_to_breakeven: int = 0
    total_partial_profit: float = 0.0
    updated_at: str = ""


@dataclass
class TimeframeSignalStats:
    """Multi-timeframe signal generation statistics."""

    m5_signals_1h: int = 0
    m5_signals_24h: int = 0
    h1_signals_1h: int = 0
    h1_signals_24h: int = 0
    mtf_aligned_signals: int = 0
    mtf_checked_signals: int = 0
    signal_ratio_m5_h1: float = 0.0
    updated_at: str = ""


@dataclass
class TradeEfficiencyStats:
    """Trade execution and duration efficiency statistics."""

    total_trades: int = 0
    total_duration_hours: float = 0.0
    avg_duration_hours: float = 0.0
    trades_under_2h: int = 0
    trades_2h_24h: int = 0
    trades_over_24h: int = 0
    duration_efficiency_score: float = 0.0
    updated_at: str = ""


@dataclass
class VolumeExecutionStats:
    """Volume sizing and execution accuracy statistics."""

    total_orders: int = 0
    target_volume_sum: float = 0.0
    actual_volume_sum: float = 0.0
    volume_accuracy_pct: float = 0.0
    orders_within_5pct: int = 0
    orders_within_10pct: int = 0
    orders_over_20pct_deviation: int = 0
    updated_at: str = ""


class V5MetricsCollector:
    """Collects v5 feature metrics for enhanced performance monitoring."""

    def __init__(self) -> None:
        """Initialize metrics collector."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize stats objects
        self.partial_exit_stats = PartialExitStats()
        self.timeframe_stats = TimeframeSignalStats()
        self.trade_efficiency_stats = TradeEfficiencyStats()
        self.volume_stats = VolumeExecutionStats()

        # Load existing stats
        self._load_existing_stats()

    def record_partial_exit(self, trade_id: str, partial_profit: float, remaining_position: bool = True) -> None:
        """Record a partial exit event.

        Args:
            trade_id: Unique trade identifier
            partial_profit: Profit from the partial exit
            remaining_position: Whether position is still open after partial exit
        """
        self.partial_exit_stats.partial_exits_count += 1
        self.partial_exit_stats.total_partial_profit += partial_profit

        # Recalculate average
        if self.partial_exit_stats.partial_exits_count > 0:
            self.partial_exit_stats.partial_avg_profit = (
                self.partial_exit_stats.total_partial_profit / self.partial_exit_stats.partial_exits_count
            )

        self.partial_exit_stats.updated_at = self._get_timestamp()
        self._persist_partial_exit_stats()
        log.debug(f"Recorded partial exit for trade {trade_id}: profit={partial_profit:.2f}")

    def record_trade_completion(
        self,
        trade_id: str,
        had_partial_exit: bool,
        total_profit: float,
        duration_hours: float,
        reached_breakeven_after_partial: bool = False,
    ) -> None:
        """Record trade completion with partial exit status.

        Args:
            trade_id: Unique trade identifier
            had_partial_exit: Whether trade had a partial exit
            total_profit: Final trade profit
            duration_hours: Trade duration in hours
            reached_breakeven_after_partial: Whether remaining position reached breakeven
        """
        # Update partial exit stats
        self.partial_exit_stats.total_trades += 1
        if had_partial_exit:
            self.partial_exit_stats.partial_trades += 1
            if reached_breakeven_after_partial:
                self.partial_exit_stats.partial_to_breakeven += 1
        else:
            # Update full trade average profit
            full_count = self.partial_exit_stats.total_trades - self.partial_exit_stats.partial_trades
            if full_count > 0:
                self.partial_exit_stats.full_avg_profit = (
                    self.partial_exit_stats.full_avg_profit * (full_count - 1) + total_profit
                ) / full_count

        # Update trade duration stats
        self.trade_efficiency_stats.total_trades += 1
        self.trade_efficiency_stats.total_duration_hours += duration_hours
        self.trade_efficiency_stats.avg_duration_hours = (
            self.trade_efficiency_stats.total_duration_hours / self.trade_efficiency_stats.total_trades
        )

        # Categorize by duration
        if duration_hours < 2.0:
            self.trade_efficiency_stats.trades_under_2h += 1
        elif duration_hours <= 24.0:
            self.trade_efficiency_stats.trades_2h_24h += 1
        else:
            self.trade_efficiency_stats.trades_over_24h += 1

        # Update efficiency score (optimal range: 2-24h for IRB)
        self._update_duration_efficiency_score()

        # Update timestamps
        self.partial_exit_stats.updated_at = self._get_timestamp()
        self.trade_efficiency_stats.updated_at = self._get_timestamp()

        # Persist all updates
        self._persist_partial_exit_stats()
        self._persist_trade_efficiency_stats()

        log.debug(
            f"Recorded trade {trade_id}: partial={had_partial_exit}, "
            f"profit={total_profit:.2f}, duration={duration_hours:.1f}h"
        )

    def record_signal_generation(self, timeframe: str, signal_type: str, mtf_aligned: bool = False) -> None:
        """Record signal generation for timeframe analysis.

        Args:
            timeframe: Signal timeframe (M5, H1, etc.)
            signal_type: Signal type (entry, exit, modify)
            mtf_aligned: Whether signal was aligned with higher timeframe
        """
        if timeframe == "M5":
            self.timeframe_stats.m5_signals_1h += 1
            self.timeframe_stats.m5_signals_24h += 1
        elif timeframe == "H1":
            self.timeframe_stats.h1_signals_1h += 1
            self.timeframe_stats.h1_signals_24h += 1

        # Update MTF alignment
        if signal_type == "entry":
            self.timeframe_stats.mtf_checked_signals += 1
            if mtf_aligned:
                self.timeframe_stats.mtf_aligned_signals += 1

        # Update signal ratio
        if self.timeframe_stats.h1_signals_24h > 0:
            self.timeframe_stats.signal_ratio_m5_h1 = (
                self.timeframe_stats.m5_signals_24h / self.timeframe_stats.h1_signals_24h
            )

        self.timeframe_stats.updated_at = self._get_timestamp()
        self._persist_timeframe_stats()

    def record_volume_execution(self, target_volume: float, actual_volume: float) -> None:
        """Record volume execution accuracy.

        Args:
            target_volume: Risk-calculated target position size
            actual_volume: Actual executed position size
        """
        self.volume_stats.total_orders += 1
        self.volume_stats.target_volume_sum += target_volume
        self.volume_stats.actual_volume_sum += actual_volume

        # Calculate accuracy percentage
        if self.volume_stats.target_volume_sum > 0:
            self.volume_stats.volume_accuracy_pct = (
                self.volume_stats.actual_volume_sum / self.volume_stats.target_volume_sum
            ) * 100.0

        # Categorize accuracy
        if target_volume > 0:
            deviation_pct = abs((actual_volume - target_volume) / target_volume) * 100.0
            if deviation_pct <= 5.0:
                self.volume_stats.orders_within_5pct += 1
            elif deviation_pct <= 10.0:
                self.volume_stats.orders_within_10pct += 1
            elif deviation_pct >= 20.0:
                self.volume_stats.orders_over_20pct_deviation += 1

        self.volume_stats.updated_at = self._get_timestamp()
        self._persist_volume_stats()

        log.debug(f"Recorded volume execution: target={target_volume:.3f}, actual={actual_volume:.3f}")

    def reset_hourly_counters(self) -> None:
        """Reset hourly signal counters (call every hour)."""
        self.timeframe_stats.m5_signals_1h = 0
        self.timeframe_stats.h1_signals_1h = 0
        self._persist_timeframe_stats()

    def get_summary_stats(self) -> dict[str, Any]:
        """Get summary of all v5 metrics for reporting."""
        return {
            "partial_exits": asdict(self.partial_exit_stats),
            "timeframe_signals": asdict(self.timeframe_stats),
            "trade_efficiency": asdict(self.trade_efficiency_stats),
            "volume_execution": asdict(self.volume_stats),
            "summary_timestamp": self._get_timestamp(),
        }

    # Private helper methods

    def _update_duration_efficiency_score(self) -> None:
        """Update trade duration efficiency score."""
        if self.trade_efficiency_stats.total_trades == 0:
            return

        # Score based on distribution in optimal range (2-24h)
        total = self.trade_efficiency_stats.total_trades
        optimal = self.trade_efficiency_stats.trades_2h_24h
        too_short = self.trade_efficiency_stats.trades_under_2h
        too_long = self.trade_efficiency_stats.trades_over_24h

        optimal_pct = optimal / total
        penalty = (too_short + too_long) / total * 0.5  # 50% penalty for out-of-range

        self.trade_efficiency_stats.duration_efficiency_score = max(0.0, optimal_pct - penalty) * 100.0

    def _get_timestamp(self) -> str:
        """Get current UTC timestamp."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _load_existing_stats(self) -> None:
        """Load existing stats from STATE files."""
        try:
            # Load partial exit stats
            pe_file = STATE_DIR / "partial_exit_stats.json"
            if pe_file.exists():
                data = json.loads(pe_file.read_text())
                self.partial_exit_stats = PartialExitStats(**data)

            # Load timeframe stats
            tf_file = STATE_DIR / "signal_stats.json"
            if tf_file.exists():
                data = json.loads(tf_file.read_text())
                # Only load timeframe-related fields
                tf_fields = {k: v for k, v in data.items() if k in TimeframeSignalStats.__dataclass_fields__}
                if tf_fields:
                    self.timeframe_stats = TimeframeSignalStats(**tf_fields)

            # Load trade efficiency stats
            te_file = STATE_DIR / "trade_stats.json"
            if te_file.exists():
                data = json.loads(te_file.read_text())
                self.trade_efficiency_stats = TradeEfficiencyStats(**data)

            # Load volume stats
            vol_file = STATE_DIR / "volume_stats.json"
            if vol_file.exists():
                data = json.loads(vol_file.read_text())
                self.volume_stats = VolumeExecutionStats(**data)

        except (json.JSONDecodeError, TypeError) as e:
            log.warning(f"Failed to load existing v5 metrics: {e}")

    def _persist_partial_exit_stats(self) -> None:
        """Persist partial exit stats to STATE file."""
        try:
            file_path = STATE_DIR / "partial_exit_stats.json"
            file_path.write_text(json.dumps(asdict(self.partial_exit_stats), indent=2))
        except OSError as e:
            log.error(f"Failed to persist partial exit stats: {e}")

    def _persist_timeframe_stats(self) -> None:
        """Persist timeframe stats to STATE file."""
        try:
            file_path = STATE_DIR / "signal_stats.json"
            # Load existing signal_stats and merge timeframe data
            existing = {}
            if file_path.exists():
                existing = json.loads(file_path.read_text())
            existing.update(asdict(self.timeframe_stats))
            file_path.write_text(json.dumps(existing, indent=2))
        except OSError as e:
            log.error(f"Failed to persist timeframe stats: {e}")

    def _persist_trade_efficiency_stats(self) -> None:
        """Persist trade efficiency stats to STATE file."""
        try:
            file_path = STATE_DIR / "trade_stats.json"
            file_path.write_text(json.dumps(asdict(self.trade_efficiency_stats), indent=2))
        except OSError as e:
            log.error(f"Failed to persist trade efficiency stats: {e}")

    def _persist_volume_stats(self) -> None:
        """Persist volume execution stats to STATE file."""
        try:
            file_path = STATE_DIR / "volume_stats.json"
            file_path.write_text(json.dumps(asdict(self.volume_stats), indent=2))
        except OSError as e:
            log.error(f"Failed to persist volume stats: {e}")


# Global collector instance for easy access
_collector_instance: V5MetricsCollector | None = None


def get_metrics_collector() -> V5MetricsCollector:
    """Get the global v5 metrics collector instance."""
    global _collector_instance
    if _collector_instance is None:
        _collector_instance = V5MetricsCollector()
    return _collector_instance
