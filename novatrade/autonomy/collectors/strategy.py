"""Strategy Validity collector — trade activity, signal generation, alignment."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class StrategyCollector(BaseCollector):
    """Measures strategy validity: trade activity, silent failures, alignment."""

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        # --- trades_last_24h ---
        try:
            trade_score, trade_raw = self._check_trade_count()
            sub_metrics.append(
                SubMetric(
                    name="trades_last_24h",
                    value=self._safe_score(trade_score),
                    raw_value=trade_raw,
                    description="Trade count in last 24 hours",
                )
            )
        except Exception as exc:
            self.log.warning("trades_last_24h failed: %s", exc)
            warnings.append(f"trades_last_24h failed: {exc}")
            sub_metrics.append(SubMetric(name="trades_last_24h", value=0.0))

        # --- silent_failure_detected ---
        try:
            sf_score, sf_raw = self._check_silent_failure()
            sub_metrics.append(
                SubMetric(
                    name="silent_failure_detected",
                    value=self._safe_score(sf_score),
                    raw_value=sf_raw,
                    description="No signals during market hours = silent failure",
                )
            )
        except Exception as exc:
            self.log.warning("silent_failure_detected failed: %s", exc)
            warnings.append(f"silent_failure_detected failed: {exc}")
            sub_metrics.append(SubMetric(name="silent_failure_detected", value=0.0))

        # --- backtest_live_alignment ---
        try:
            bt_score, bt_raw = self._check_backtest_alignment()
            sub_metrics.append(
                SubMetric(
                    name="backtest_live_alignment",
                    value=self._safe_score(bt_score),
                    raw_value=bt_raw,
                    description="Backtest vs live performance alignment",
                )
            )
        except Exception as exc:
            self.log.warning("backtest_live_alignment failed: %s", exc)
            warnings.append(f"backtest_live_alignment failed: {exc}")
            sub_metrics.append(SubMetric(name="backtest_live_alignment", value=0.0))

        # --- signal_generation_rate ---
        try:
            sg_score, sg_raw = self._check_signal_rate()
            sub_metrics.append(
                SubMetric(
                    name="signal_generation_rate",
                    value=self._safe_score(sg_score),
                    raw_value=sg_raw,
                    description="Signals per hour during market hours",
                )
            )
        except Exception as exc:
            self.log.warning("signal_generation_rate failed: %s", exc)
            warnings.append(f"signal_generation_rate failed: {exc}")
            sub_metrics.append(SubMetric(name="signal_generation_rate", value=0.0))

        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        return DimensionScore(
            name="Strategy Validity",
            score=round(avg, 1),
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _load_trade_log(self) -> list[dict]:
        """Load trade log from STATE/novatrade/trade_log.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "trade_log.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _check_trade_count(self) -> tuple[float, float]:
        """Count trades in last 24h."""
        trades = self._load_trade_log()
        cutoff = time.time() - 24 * 3600

        recent = [t for t in trades if t.get("timestamp", 0) >= cutoff]
        count = len(recent)

        if count >= 5:
            score = 100.0
        elif count >= 2:
            score = 80.0
        elif count >= 1:
            score = 60.0
        else:
            # 0 trades — might be normal (market closed), not critical
            score = 20.0

        return score, float(count)

    def _check_silent_failure(self) -> tuple[float, float]:
        """Detect silent failure: market hours + no signals for 4+ hours."""
        now = datetime.now(timezone.utc)

        # Simple market hours check (Mon-Fri, 07:00-21:00 UTC covers London+NY)
        if now.weekday() >= 5:
            # Weekend — no trading expected
            return 100.0, 0.0

        if now.hour < 7 or now.hour >= 21:
            # Off-hours
            return 100.0, 0.0

        # During market hours — check signal freshness
        output_dir = Path(self.base_path) / "OUTPUT"
        if not output_dir.is_dir():
            return 0.0, 1.0  # no output dir during market hours = failure

        newest_mtime = 0.0
        for p in output_dir.iterdir():
            if not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
                if mt > newest_mtime:
                    newest_mtime = mt
            except OSError:
                continue

        if newest_mtime == 0.0:
            return 0.0, 1.0

        age_h = (time.time() - newest_mtime) / 3600.0
        if age_h > 4:
            return 0.0, age_h  # silent failure detected
        return 100.0, 0.0

    def _check_backtest_alignment(self) -> tuple[float, float]:
        """Check backtest vs live alignment. Placeholder if no data."""
        bt_dir = Path(self.base_path) / "OUTPUT" / "backtests"
        if not bt_dir.is_dir():
            return 50.0, -1.0  # no backtest data — neutral

        # Check for any recent backtest result
        results = list(bt_dir.glob("*.json"))
        if not results:
            return 50.0, -1.0

        # Placeholder: backtest exists = moderate confidence
        return 50.0, float(len(results))

    def _check_signal_rate(self) -> tuple[float, float]:
        """Signals per hour during market hours."""
        now = datetime.now(timezone.utc)

        # Only relevant during market hours
        if now.weekday() >= 5 or now.hour < 7 or now.hour >= 21:
            return 80.0, -1.0  # off-hours, assume OK

        trades = self._load_trade_log()
        cutoff = time.time() - 3600  # last hour

        recent_signals = [t for t in trades if t.get("timestamp", 0) >= cutoff]
        rate = float(len(recent_signals))
        score = min(100.0, rate * 20.0)

        # If rate is 0 during market hours, score is 0
        return score, rate
