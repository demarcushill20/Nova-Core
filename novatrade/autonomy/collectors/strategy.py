"""Strategy Validity collector — trade activity, signal generation, alignment."""

from __future__ import annotations

import json
import os
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

        # Compute confidence based on data freshness
        confidence = self._compute_confidence()

        return DimensionScore(
            name="Strategy Validity",
            score=round(avg, 1),
            confidence=confidence,
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    def _compute_confidence(self) -> float:
        """Compute confidence based on trade journal and signal data freshness.

        - trade_journal exists and fresh (<30min) → 1.0
        - trade_journal exists, slightly stale (30min-2h) → 0.8
        - trade_journal exists but stale (2-6h) → 0.6
        - trade_journal exists but very stale (>6h) → 0.3
        - no trade_journal but signal_log exists → 0.5
        - no data at all → 0.1
        """
        now = time.time()

        # Check trade_journal.jsonl
        journal_path = Path(self.base_path) / "STATE" / "novatrade" / "trade_journal.jsonl"
        if journal_path.exists():
            try:
                age_h = (now - journal_path.stat().st_mtime) / 3600.0
                if age_h < 0.5:
                    return 1.0
                elif age_h < 2:
                    return 0.8
                elif age_h < 6:
                    return 0.6
                else:
                    return 0.3
            except OSError:
                pass

        # Fallback: check signal_log.json
        signal_path = Path(self.base_path) / "STATE" / "novatrade" / "signal_log.json"
        if signal_path.exists():
            try:
                age_h = (now - signal_path.stat().st_mtime) / 3600.0
                if age_h < 2:
                    return 0.5
                else:
                    return 0.3
            except OSError:
                pass

        # No strategy data at all
        return 0.1

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_shadow_symbol(symbol: str) -> bool:
        """Return True if symbol indicates shadow/dry-run mode.

        FTMO demo accounts use ".sim" as their real broker symbol suffix
        (configured via FTMO_SYMBOL_SUFFIX env var). When the suffix
        matches the configured broker suffix, the trade is real.
        """
        if not symbol.endswith(".sim"):
            return False
        ftmo_suffix = os.environ.get("FTMO_SYMBOL_SUFFIX", "")
        return not (ftmo_suffix and symbol.endswith(ftmo_suffix))

    def _load_trade_log(self) -> list[dict]:
        """Load trades from STATE/novatrade/trade_journal.jsonl.

        Reads the append-only JSONL trade journal (source of truth) and
        returns OPEN/CLOSE events with a numeric ``timestamp`` field
        derived from the ISO ``logged_at`` field for backward compat.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "trade_journal.jsonl"
        if not path.exists():
            return []
        trades: list[dict] = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = entry.get("event", "")
                    if event not in ("OPEN", "CLOSE"):
                        continue
                    # Convert logged_at ISO string to numeric timestamp
                    logged_at = entry.get("logged_at", "")
                    if logged_at:
                        try:
                            from datetime import datetime as _dt

                            dt = _dt.fromisoformat(logged_at)
                            entry["timestamp"] = dt.timestamp()
                        except (ValueError, TypeError):
                            entry["timestamp"] = 0
                    trades.append(entry)
        except OSError:
            return []
        return trades

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

        recent = [
            t
            for t in trades
            if t.get("timestamp", 0) >= cutoff and not self._is_shadow_symbol(str(t.get("symbol", "")))
        ]
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
        """Detect silent failure with regime-aware thresholds.

        Uses persisted regime classification and session leniency to adjust
        silence thresholds instead of a flat 4-hour check.  During ranging/quiet
        regimes or low-activity sessions, longer silence is expected and scored
        accordingly.
        """
        now = datetime.now(timezone.utc)

        # Weekend — no trading expected
        if now.weekday() >= 5:
            return 85.0, 0.0

        # Load regime from persisted state (written by live_loop health monitor)
        regime = self._load_current_regime()

        # Import regime thresholds and session detection
        from novatrade.monitor.signal_monitor import (
            REGIME_MULTIPLIERS,
            SESSION_LENIENCY,
            get_session,
        )

        session = get_session(now)
        if session == "weekend":
            return 85.0, 0.0

        thresholds = REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["ranging"])
        leniency = SESSION_LENIENCY.get(session, 1.0)

        # Apply session leniency to stale thresholds (wider during Asian)
        stale_red_hours = thresholds["stale_red_hours"] / max(leniency, 0.1)

        # Check signal_stats.json first (most current if live runtime is persisting)
        signal_rate = self._load_signal_stats_rate()
        if signal_rate > 0:
            return 100.0, 0.0

        # Fallback: check trade_journal.jsonl and signal_log.json
        cutoff_red = time.time() - stale_red_hours * 3600

        # 1. Check trade_journal.jsonl for recent trade events
        trades = self._load_trade_log()
        recent_trades = [t for t in trades if t.get("timestamp", 0) >= cutoff_red]
        if recent_trades:
            return 100.0, 0.0  # trades within regime-aware window

        # 2. Check signal_log.json for recent signals
        signals = self._load_signal_log()
        recent_signals = sum(1 for s in signals if s.get("timestamp", 0) >= cutoff_red)
        if recent_signals > 0:
            return 100.0, 0.0  # signals within regime-aware window

        # 3. Check pipeline liveness — ticks/bars flowing = strategy waiting, not crashed
        live_metrics = self._load_live_metrics()
        if live_metrics and live_metrics.get("ticks", 0) > 0:
            return 40.0, stale_red_hours  # pipeline alive, strategy just has no setups

        # No activity beyond regime-aware threshold → silent failure
        return 0.0, stale_red_hours

    def _check_backtest_alignment(self) -> tuple[float, float]:
        """Check backtest vs live performance alignment.

        Reads backtest Sharpe from OUTPUT/backtests/*.json and live Sharpe
        from STATE/novatrade/equity_history.json.  Scores based on the
        relative delta between the two.

        Returns (score, delta) where delta is the relative divergence.
        No data on either side → neutral 50.
        Pre-live (all equity snapshots from backtest, no real trades) → 60.
        Off-hours/weekends → neutral 70 (no trading activity to measure).
        """
        now = datetime.now(timezone.utc)

        # Off-hours: Sharpe comparison is meaningless when markets are closed.
        # Equity snapshots during weekends/off-hours are just balance polls,
        # not trading outcomes — comparing them to backtest Sharpe is noise.
        if now.weekday() >= 5 or now.hour < 7 or now.hour >= 21:
            return 70.0, -4.0  # -4 sentinel = off-hours

        bt_sharpe = self._load_backtest_sharpe()
        live_sharpe = self._load_live_sharpe()

        # No backtest data → neutral score
        if bt_sharpe is None:
            return 50.0, -1.0

        # Backtest exists but not enough live data → credit for having backtests
        if live_sharpe is None:
            return 60.0, -2.0

        # Pre-live check: if all equity snapshots are from backtest runs,
        # there's no real live/backtest divergence to measure yet.
        if self._is_pre_live():
            return 60.0, -3.0  # -3 sentinel = pre-live state

        # Flat-equity guard: if live Sharpe is near zero (|Sharpe| < 0.01),
        # equity is just balance polling with no meaningful P&L — comparing
        # to backtest Sharpe is noise.  This catches both no-trade scenarios
        # and shadow-mode-only scenarios where journal has sim entries.
        if abs(live_sharpe) < 0.01:
            return 60.0, -5.0  # -5 sentinel = no meaningful live trading data

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

    def _is_pre_live(self) -> bool:
        """Return True if equity_history has no real live trading data.

        Checks whether all snapshots have source='backtest' (or no source),
        indicating no MetaApi live equity has been recorded yet.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "equity_history.json"
        if not path.exists():
            return True
        try:
            data = json.loads(path.read_text())
            snapshots = data if isinstance(data, list) else data.get("snapshots", [])
            if not snapshots:
                return True
            for s in snapshots:
                if isinstance(s, dict):
                    source = s.get("source", "backtest")
                    if source not in ("backtest", "simulation"):
                        return False  # found real live data
            return True  # all entries are backtest/simulation
        except (json.JSONDecodeError, OSError):
            return True

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
        """Compute live Sharpe from STATE/novatrade/equity_history.json.

        Filters outlier equity values (test artifacts, simulation spikes)
        using median-absolute-deviation before computing returns.
        """
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

            # Filter outliers: remove values > 3× MAD from median.
            # Equity history can contain test artifacts (e.g. 5000, 95000)
            # that create artificial volatility and corrupt the Sharpe ratio.
            sorted_eq = sorted(equities)
            median = sorted_eq[len(sorted_eq) // 2]
            if median > 0:
                abs_devs = [abs(e - median) for e in equities]
                abs_devs_sorted = sorted(abs_devs)
                mad = abs_devs_sorted[len(abs_devs_sorted) // 2]
                # MAD-based threshold: 3× MAD, minimum 1% of median
                threshold = max(3.0 * mad, median * 0.01)
                equities = [e for e in equities if abs(e - median) <= threshold]

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

        # If signal_log.json has no recent entries, check signal_stats.json
        # as a secondary source.  signal_log.json is a bounded ring buffer
        # that can go stale while the runtime continues generating signals.
        if total == 0:
            stats_total = self._load_signal_stats_rate()
            if stats_total > 0:
                total = stats_total

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

    def _load_signal_stats_rate(self) -> int:
        """Read signal_stats.json for real-time signal counts.

        signal_stats.json is updated every tick by the live runtime and
        contains ``signals_1h`` — the number of signals in the last hour.
        Unlike signal_log.json (a bounded ring buffer), this file stays
        current as long as the runtime is alive.
        """
        path = Path(self.base_path) / "STATE" / "novatrade" / "signal_stats.json"
        if not path.exists():
            return 0
        try:
            # Only trust the file if it was written in the last 10 minutes
            age_min = (time.time() - path.stat().st_mtime) / 60.0
            if age_min > 10:
                return 0
            data = json.loads(path.read_text())
            return int(data.get("signals_1h", 0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return 0

    def _load_current_regime(self) -> str:
        """Load current regime from STATE/novatrade/regime.json."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "regime.json"
        if not path.exists():
            return "ranging"
        try:
            age_min = (time.time() - path.stat().st_mtime) / 60.0
            if age_min > 30:  # stale regime — fall back to default
                return "ranging"
            data = json.loads(path.read_text())
            return data.get("regime", "ranging")
        except (json.JSONDecodeError, OSError):
            return "ranging"

    def _load_live_metrics(self) -> dict | None:
        """Load live_metrics.json for pipeline liveness check."""
        path = Path(self.base_path) / "STATE" / "novatrade" / "live_metrics.json"
        if not path.exists():
            return None
        try:
            age_min = (time.time() - path.stat().st_mtime) / 60.0
            if age_min > 10:
                return None
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

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

        # Check 4: No halt/error state — read the canonical risk-engine
        # state file. (Legacy ``halt_state.json`` had no writer anywhere.)
        risk_state_path = Path(self.base_path) / "STATE" / "novatrade_risk_state.json"
        if risk_state_path.exists():
            try:
                rs_data = json.loads(risk_state_path.read_text())
                if rs_data.get("halted") or rs_data.get("breached"):
                    pass  # halted — don't add points
                else:
                    score += 25.0
                    checks_passed += 1
            except (json.JSONDecodeError, OSError):
                score += 25.0  # can't read → assume OK
                checks_passed += 1
        else:
            score += 25.0  # no risk-state file → assume not halted
            checks_passed += 1

        return score, float(checks_passed)
