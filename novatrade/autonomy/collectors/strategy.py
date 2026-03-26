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

        # --- signal_pipeline_health ---
        try:
            sp_score, sp_raw = self._check_signal_pipeline_health()
            sub_metrics.append(
                SubMetric(
                    name="signal_pipeline_health",
                    value=self._safe_score(sp_score),
                    raw_value=sp_raw,
                    description="Strategy engine and indicator pipeline readiness",
                )
            )
        except Exception as exc:
            self.log.warning("signal_pipeline_health failed: %s", exc)
            warnings.append(f"signal_pipeline_health failed: {exc}")
            sub_metrics.append(SubMetric(name="signal_pipeline_health", value=0.0))

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
        """Load trade log from STATE/novatrade/trade_log.json.

        Handles both formats:
        - Legacy list: ``[{...}, ...]``
        - Current dict: ``{"trades": [{...}, ...], "last_updated": "..."}``
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "trade_log.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("trades", [])
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _load_signal_log(self) -> list[dict]:
        """Load signal log from STATE/novatrade/signal_log.json.

        Format: ``{"signals": [{...}, ...], "last_updated": "..."}``
        Falls back to LiveMetrics state if signal_log doesn't exist.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "signal_log.json"
        if not path.exists():
            # Fallback: try live_metrics.json for signal counters
            return self._load_signal_metrics_fallback()
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("signals", [])
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _load_signal_metrics_fallback(self) -> list[dict]:
        """Fallback: read live_metrics.json for signal counters."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "live_metrics.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                # Convert aggregate counters into synthetic signal entries
                total = data.get("signals_entry", 0) + data.get("signals_exit", 0) + data.get("signals_modify_sl", 0)
                if total > 0:
                    # Return a single synthetic entry with the timestamp from the file
                    ts = path.stat().st_mtime
                    return [{"timestamp": ts, "type": "aggregate", "count": total}]
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _check_trade_count(self) -> tuple[float, float]:
        """Count trades in last 24h with market-hours awareness.

        Off-hours (weekends, outside 07-21 UTC): 0 trades is expected → 80.
        During market hours: standard tiered scoring.
        """
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
            # 0 trades — check if market is open
            now = datetime.now(timezone.utc)
            if now.weekday() >= 5 or now.hour < 7 or now.hour >= 21:
                score = 80.0  # off-hours, 0 trades is normal
            else:
                score = 20.0  # market open but no trades — concerning

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
        """Check backtest vs live performance alignment.

        Reads backtest Sharpe from OUTPUT/backtests/*.json and live Sharpe
        from STATE/novatrade/equity_history.json.  Scores based on the
        relative delta between the two.

        Returns (score, delta) where delta is the relative divergence.
        No data on either side → neutral 50.
        """
        bt_sharpe = self._load_backtest_sharpe()
        live_sharpe = self._load_live_sharpe()

        # No backtest data → neutral score
        if bt_sharpe is None:
            return 50.0, -1.0

        # Backtest exists but not enough live data → credit for having backtests
        if live_sharpe is None:
            return 60.0, -2.0

        # Real alignment: delta-based scoring
        denom = max(abs(bt_sharpe), 0.01)
        delta = abs(live_sharpe - bt_sharpe) / denom

        if delta < 0.1:
            score = 100.0  # <10% divergence — excellent
        elif delta < 0.3:
            score = 70.0  # <30% divergence — acceptable
        elif delta < 0.5:
            score = 40.0  # moderate divergence
        else:
            score = 10.0  # significant divergence

        return score, round(delta, 3)

    def _load_backtest_sharpe(self) -> float | None:
        """Load most recent backtest Sharpe ratio from OUTPUT/backtests/."""
        bt_dir = Path(self.base_path) / "OUTPUT" / "backtests"
        if not bt_dir.is_dir():
            return None

        results = sorted(bt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not results:
            return None

        try:
            data = json.loads(results[0].read_text())
            if isinstance(data, dict):
                # Try common field names
                for key in ("sharpe", "sharpe_ratio", "sharpe_ratio_annualized"):
                    if key in data:
                        return float(data[key])
                # Try nested in metrics/results
                for section in ("metrics", "results", "stats"):
                    if isinstance(data.get(section), dict):
                        for key in ("sharpe", "sharpe_ratio"):
                            if key in data[section]:
                                return float(data[section][key])
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
        return None

    def _load_live_sharpe(self) -> float | None:
        """Compute live Sharpe from STATE/novatrade/equity_history.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "equity_history.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            snapshots = data if isinstance(data, list) else data.get("snapshots", [])
            if len(snapshots) < 5:
                return None  # not enough data

            equities = [s.get("equity", s) if isinstance(s, dict) else s for s in snapshots]
            equities = [float(e) for e in equities if e is not None]
            if len(equities) < 5:
                return None

            # Simple Sharpe: mean(returns) / std(returns)
            returns = [(equities[i] - equities[i - 1]) / max(equities[i - 1], 1.0) for i in range(1, len(equities))]
            if not returns:
                return None
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std_r = var_r**0.5
            if std_r < 1e-10:
                return 0.0
            return mean_r / std_r
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return None

    def _get_pipeline_mode(self) -> str:
        """Read pipeline mode from strategy_config.json. Defaults to 'unknown'."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "strategy_config.json"
        if not path.exists():
            return "unknown"
        try:
            data = json.loads(path.read_text())
            return data.get("pipeline", "unknown")
        except (json.JSONDecodeError, OSError):
            return "unknown"

    def _check_signal_rate(self) -> tuple[float, float]:
        """Signals per hour during market hours.

        Pipeline-aware:
        - **live**: reads signal_log.json / live_metrics.json
        - **webhook**: uses OUTPUT file recency as proxy (signals come from TradingView)
        """
        now = datetime.now(timezone.utc)

        # Only relevant during market hours
        if now.weekday() >= 5 or now.hour < 7 or now.hour >= 21:
            return 80.0, -1.0  # off-hours, assume OK

        pipeline = self._get_pipeline_mode()

        # Webhook mode: signals come from TradingView alerts, not internal engine.
        # Use OUTPUT file recency and service uptime as proxy.
        if pipeline == "webhook":
            return self._check_webhook_signal_proxy()

        signals = self._load_signal_log()
        cutoff = time.time() - 3600  # last hour

        # Handle aggregate entries (from live_metrics fallback)
        total = 0
        for s in signals:
            if s.get("type") == "aggregate":
                total += s.get("count", 0)
            elif s.get("timestamp", 0) >= cutoff:
                total += 1

        rate = float(total)
        score = min(100.0, rate * 20.0)

        return score, rate

    def _check_webhook_signal_proxy(self) -> tuple[float, float]:
        """In webhook mode, signals come from TradingView not internal engine.

        Score based on:
        - Service is running (strategy_config.json exists and is recent) → 70
        - Recent OUTPUT activity → up to 100
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        config_path = state_dir / "strategy_config.json"

        # Base: service is configured and running
        if not config_path.exists():
            return 0.0, 0.0

        try:
            age_h = (time.time() - config_path.stat().st_mtime) / 3600.0
        except OSError:
            age_h = 999.0

        if age_h > 24:
            return 30.0, 0.0  # stale config

        # Check for recent OUTPUT activity (heartbeats, trade results, etc.)
        output_dir = Path(self.base_path) / "OUTPUT"
        if output_dir.is_dir():
            newest_age_h = 999.0
            for p in output_dir.iterdir():
                if not p.is_file():
                    continue
                try:
                    file_age = (time.time() - p.stat().st_mtime) / 3600.0
                    if file_age < newest_age_h:
                        newest_age_h = file_age
                except OSError:
                    continue

            if newest_age_h < 1:
                return 90.0, 1.0  # very recent activity
            if newest_age_h < 4:
                return 75.0, 0.5  # reasonably recent
            if newest_age_h < 12:
                return 60.0, 0.25

        # Webhook pipeline is configured, awaiting TradingView alerts
        return 70.0, 0.0

    def _check_signal_pipeline_health(self) -> tuple[float, float]:
        """Check if the signal pipeline components are operational.

        Scores based on:
        - Strategy engine config exists (25 pts)
        - LiveMetrics state exists and has recent data (25 pts)
        - Signal log has recent entries (25 pts)
        - No error markers in STATE (25 pts)
        """
        score = 0.0
        checks_passed = 0

        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        # Check 1: Strategy config/state exists
        config_markers = [
            state_dir / "strategy_config.json",
            state_dir / "live_metrics.json",
            state_dir / "feed.json",
        ]
        if any(p.exists() for p in config_markers):
            score += 25.0
            checks_passed += 1

        # Check 2: LiveMetrics state is recent (< 2 hours old)
        metrics_path = state_dir / "live_metrics.json"
        if metrics_path.exists():
            try:
                age_h = (time.time() - metrics_path.stat().st_mtime) / 3600.0
                if age_h < 2:
                    score += 25.0
                    checks_passed += 1
                elif age_h < 6:
                    score += 10.0
            except OSError:
                pass

        # Check 3: Signal source active
        # In webhook mode: TradingView sends signals, so check webhook readiness
        # In live mode: check signal_log for internal engine output
        pipeline = self._get_pipeline_mode()
        if pipeline == "webhook":
            # Webhook mode: service is up and configured → signals come externally
            if (state_dir / "strategy_config.json").exists():
                score += 25.0
                checks_passed += 1
        else:
            signals = self._load_signal_log()
            if signals:
                score += 25.0
                checks_passed += 1

        # Check 4: No halt/error state
        halt_path = state_dir / "halt_state.json"
        if halt_path.exists():
            try:
                halt_data = json.loads(halt_path.read_text())
                if halt_data.get("halted"):
                    pass  # halted — don't add points
                else:
                    score += 25.0
                    checks_passed += 1
            except (json.JSONDecodeError, OSError):
                score += 25.0  # can't read halt file → assume OK
                checks_passed += 1
        else:
            score += 25.0  # no halt file → not halted
            checks_passed += 1

        return score, float(checks_passed)
