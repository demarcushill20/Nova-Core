"""Execution Pipeline collector — connectivity, rejections, signal freshness."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class PipelineCollector(BaseCollector):
    """Measures execution pipeline health: connectivity, rejections, signal age, feed."""

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        metrics = self._load_metrics()

        # --- pipeline_connectivity ---
        try:
            conn_score, conn_raw = self._check_connectivity(metrics)
            sub_metrics.append(
                SubMetric(
                    name="pipeline_connectivity",
                    value=self._safe_score(conn_score),
                    raw_value=conn_raw,
                    description="Contract success rate from STATE/metrics.json",
                )
            )
        except Exception as exc:
            self.log.warning("pipeline_connectivity failed: %s", exc)
            warnings.append(f"pipeline_connectivity failed: {exc}")
            sub_metrics.append(SubMetric(name="pipeline_connectivity", value=0.0))

        # --- rejection_rate ---
        try:
            rej_score, rej_raw = self._check_rejection_rate(metrics)
            sub_metrics.append(
                SubMetric(
                    name="rejection_rate",
                    value=self._safe_score(rej_score),
                    raw_value=rej_raw,
                    description="Retry/rejection ratio",
                )
            )
        except Exception as exc:
            self.log.warning("rejection_rate failed: %s", exc)
            warnings.append(f"rejection_rate failed: {exc}")
            sub_metrics.append(SubMetric(name="rejection_rate", value=0.0))

        # --- last_signal_age ---
        try:
            sig_score, sig_raw = self._check_signal_age()
            sub_metrics.append(
                SubMetric(
                    name="last_signal_age",
                    value=self._safe_score(sig_score),
                    raw_value=sig_raw,
                    description="Age (hours) of most recent OUTPUT/ signal file",
                )
            )
        except Exception as exc:
            self.log.warning("last_signal_age failed: %s", exc)
            warnings.append(f"last_signal_age failed: {exc}")
            sub_metrics.append(SubMetric(name="last_signal_age", value=0.0))

        # --- feed_health ---
        try:
            feed_score, feed_raw = self._check_feed_health()
            sub_metrics.append(
                SubMetric(
                    name="feed_health",
                    value=self._safe_score(feed_score),
                    raw_value=feed_raw,
                    description="Price feed freshness in STATE/novatrade/",
                )
            )
        except Exception as exc:
            self.log.warning("feed_health failed: %s", exc)
            warnings.append(f"feed_health failed: {exc}")
            sub_metrics.append(SubMetric(name="feed_health", value=0.0))

        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        # Compute confidence based on state file availability and freshness
        confidence = self._compute_confidence(warnings)

        return DimensionScore(
            name="Execution Pipeline",
            score=round(avg, 1),
            confidence=confidence,
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    def _compute_confidence(self, warnings: list[str]) -> float:
        """Compute confidence based on how many state files are available and fresh.

        Primary: connection_status.json, feed_health.json, last_order_attempt.json
        Fallback: live_metrics.json, signal_stats.json (actually written by NovaTrade)
        """
        now = time.time()
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        # Primary connectivity files
        primary_files = [
            state_dir / "connection_status.json",
            state_dir / "feed_health.json",
            state_dir / "last_order_attempt.json",
        ]
        # Fallback operational files (written by NovaTrade runtime)
        fallback_files = [
            state_dir / "live_metrics.json",
            state_dir / "signal_stats.json",
        ]

        available = 0
        fresh = 0
        total = len(primary_files)

        for sf in primary_files:
            if sf.exists():
                available += 1
                try:
                    if (now - sf.stat().st_mtime) / 3600.0 < 0.5:
                        fresh += 1
                except OSError:
                    pass

        # If primary files exist, use them for confidence
        if available > 0:
            freshness_ratio = fresh / total
            availability_ratio = available / total
            confidence = 0.3 * availability_ratio + 0.7 * freshness_ratio
            return round(max(0.1, min(1.0, confidence)), 2)

        # Check fallback operational files
        fb_available = 0
        fb_fresh = 0
        for sf in fallback_files:
            if sf.exists():
                fb_available += 1
                try:
                    if (now - sf.stat().st_mtime) / 3600.0 < 0.5:
                        fb_fresh += 1
                except OSError:
                    pass

        if fb_available > 0:
            # Fallback files present — moderate confidence (capped at 0.7)
            ratio = fb_fresh / len(fallback_files)
            confidence = 0.2 + 0.5 * ratio
            return round(max(0.2, min(0.7, confidence)), 2)

        warnings.append("no connectivity data — confidence very low")
        return 0.1

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _load_metrics(self) -> dict:
        """Load STATE/metrics.json."""
        path = Path(self.base_path) / "STATE" / "metrics.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _check_connectivity(self, metrics: dict) -> tuple[float, float]:
        """Success rate = success / (success + failure)."""
        success_data = metrics.get("contract_success", {})
        failure_data = metrics.get("contract_failure", {})

        success = success_data.get("_total", 0) if isinstance(success_data, dict) else 0
        failure = failure_data.get("_total", 0) if isinstance(failure_data, dict) else 0

        total = success + failure
        if total == 0:
            return 50.0, 0.0  # no data — neutral score

        rate = success / total
        return rate * 100.0, rate

    def _check_rejection_rate(self, metrics: dict) -> tuple[float, float]:
        """Rejection pct = retry_issued / total attempts."""
        retry_data = metrics.get("retry_issued", {})
        success_data = metrics.get("contract_success", {})
        failure_data = metrics.get("contract_failure", {})

        retries = retry_data.get("_total", 0) if isinstance(retry_data, dict) else 0
        success = success_data.get("_total", 0) if isinstance(success_data, dict) else 0
        failure = failure_data.get("_total", 0) if isinstance(failure_data, dict) else 0

        total = success + failure
        if total == 0:
            return 80.0, 0.0  # no data — assume OK

        rejection_pct = (retries / total) * 100.0
        score = max(0.0, 100.0 - rejection_pct)
        return score, rejection_pct

    def _check_signal_age(self) -> tuple[float, float]:
        """Score based on age of most recent OUTPUT/ file."""
        output_dir = Path(self.base_path) / "OUTPUT"
        if not output_dir.is_dir():
            return 10.0, -1.0

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
            return 10.0, -1.0

        age_h = (time.time() - newest_mtime) / 3600.0

        if age_h < 1:
            score = 100.0
        elif age_h < 6:
            score = 70.0
        elif age_h < 24:
            score = 40.0
        else:
            score = 10.0

        return score, round(age_h, 2)

    def _check_feed_health(self) -> tuple[float, float]:
        """Score based on connectivity state files and live NovaTrade metrics.

        Primary checks (if available):
        - connection_status.json → MetaApi connection state (40 pts)
        - feed_health.json → per-symbol feed freshness (30 pts)
        - last_order_attempt.json → recent order execution results (30 pts)

        Fallback checks (used when primary files are absent):
        - live_metrics.json → tick count, uptime, error rate (up to 50 pts)
        - signal_stats.json → signal status, concern level (up to 50 pts)
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        if not state_dir.is_dir():
            return 0.0, 0.0

        score = 0.0
        checks = 0

        # 1. Check connection_status.json for MetaApi connection state
        conn_path = state_dir / "connection_status.json"
        if conn_path.exists():
            try:
                data = json.loads(conn_path.read_text())
                status = data.get("status", "").lower() if isinstance(data, dict) else ""
                if status in ("connected", "active", "synchronized"):
                    score += 40.0
                    checks += 1
                elif status in ("connecting", "reconnecting"):
                    score += 20.0
                    checks += 1
                else:
                    checks += 1  # present but disconnected
            except (json.JSONDecodeError, OSError):
                pass

        # 2. Check feed_health.json for per-symbol freshness
        feed_path = state_dir / "feed_health.json"
        if feed_path.exists():
            try:
                data = json.loads(feed_path.read_text())
                if isinstance(data, dict):
                    symbols = data.get("symbols", data)
                    if isinstance(symbols, dict) and symbols:
                        stale_count = 0
                        total_symbols = 0
                        now = time.time()
                        for _sym, info in symbols.items():
                            if not isinstance(info, dict):
                                continue
                            total_symbols += 1
                            last_tick = info.get("last_tick", info.get("updated_at", 0))
                            if isinstance(last_tick, str):
                                try:
                                    last_tick = datetime.fromisoformat(last_tick).timestamp()
                                except (ValueError, TypeError):
                                    last_tick = 0
                            if now - float(last_tick or 0) > 300:  # stale if >5 min
                                stale_count += 1

                        if total_symbols > 0:
                            fresh_ratio = (total_symbols - stale_count) / total_symbols
                            score += 30.0 * fresh_ratio
                            checks += 1
                    elif isinstance(symbols, dict):
                        checks += 1  # empty symbols dict
                else:
                    checks += 1
            except (json.JSONDecodeError, OSError):
                pass

        # 3. Check last_order_attempt.json for recent order results
        order_path = state_dir / "last_order_attempt.json"
        if order_path.exists():
            try:
                data = json.loads(order_path.read_text())
                if isinstance(data, dict):
                    result = (data.get("result") or data.get("status") or "").lower()
                    if result in ("success", "filled", "executed"):
                        score += 30.0
                    elif result in ("partial", "pending"):
                        score += 15.0
                    # else: failed or rejected → 0 contribution
                    checks += 1
            except (json.JSONDecodeError, OSError):
                pass

        # If primary files gave results, return them
        if checks > 0:
            return score, float(checks)

        # --- Fallback: use live NovaTrade operational files ---
        # These files are actually written by the NovaTrade runtime.

        # 4. live_metrics.json — tick activity and error rate (up to 50 pts)
        live_path = state_dir / "live_metrics.json"
        if live_path.exists():
            try:
                age_h = (time.time() - live_path.stat().st_mtime) / 3600.0
                if age_h < 0.5:  # must be recently updated
                    data = json.loads(live_path.read_text())
                    if isinstance(data, dict):
                        ticks = data.get("ticks", 0)
                        errors = data.get("errors", 0)

                        if ticks > 0:
                            # Service is alive and receiving ticks
                            error_ratio = errors / max(ticks, 1)
                            if error_ratio < 0.05:
                                score += 50.0  # healthy — low errors
                            elif error_ratio < 0.2:
                                score += 30.0  # some errors
                            else:
                                score += 10.0  # high error rate
                            checks += 1
                        elif self._is_forex_market_closed() or self._runtime_reports_market_closed():
                            # ticks=0 expected during market closure or
                            # early Monday transition when runtime still idle
                            if errors == 0:
                                score += 45.0  # healthy idle — no errors
                            else:
                                score += 25.0  # idle but has errors
                            checks += 1
                        else:
                            score += 20.0  # file fresh → service running
                            checks += 1
            except (json.JSONDecodeError, OSError):
                pass

        # 5. signal_stats.json — signal status and concern level (up to 50 pts)
        stats_path = state_dir / "signal_stats.json"
        if stats_path.exists():
            try:
                data = json.loads(stats_path.read_text())
                if isinstance(data, dict):
                    status = data.get("status", "").upper()
                    concern = data.get("concern_level", "").upper()

                    if status == "OK" and concern == "GREEN":
                        score += 50.0
                    elif status == "MARKET_CLOSED" and concern == "GREEN":
                        score += 45.0  # healthy weekend/transition idle
                    elif status == "OK":
                        score += 35.0
                    elif concern in ("GREEN", "YELLOW"):
                        score += 20.0
                    else:
                        score += 5.0
                    checks += 1
            except (json.JSONDecodeError, OSError):
                pass

        if checks > 0:
            return score, float(checks)

        # Last resort: generic mtime check on any file in the directory
        newest_mtime = 0.0
        for p in state_dir.iterdir():
            if not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
                if mt > newest_mtime:
                    newest_mtime = mt
            except OSError:
                continue

        if newest_mtime == 0.0:
            return 0.0, 0.0

        age_h = (time.time() - newest_mtime) / 3600.0
        if age_h < 1:
            score = 50.0  # generic data, reduced score
        elif age_h < 6:
            score = 25.0
        return score, round(age_h, 2)

    def _runtime_reports_market_closed(self) -> bool:
        """Check if NovaTrade runtime's signal_stats reports MARKET_CLOSED.

        Trusts the runtime's own assessment of market state, which handles
        edge cases like early Monday transition better than static weekday checks.
        """
        stats_path = Path(self.base_path) / "STATE" / "novatrade" / "signal_stats.json"
        if not stats_path.exists():
            return False
        try:
            data = json.loads(stats_path.read_text())
            if isinstance(data, dict):
                return data.get("status", "").upper() == "MARKET_CLOSED"
        except (json.JSONDecodeError, OSError):
            pass
        return False

    @staticmethod
    def _is_forex_market_closed() -> bool:
        """Check if forex markets are currently closed.

        Forex markets close Friday ~22:00 UTC and reopen Sunday ~22:00 UTC.
        Returns True during the weekend closure window.
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()  # 0=Mon ... 6=Sun
        hour = now.hour

        if weekday == 5:  # Saturday — always closed
            return True
        if weekday == 6 and hour < 22:  # Sunday before 22:00 UTC
            return True
        return weekday == 4 and hour >= 22  # Friday after 22:00 UTC
