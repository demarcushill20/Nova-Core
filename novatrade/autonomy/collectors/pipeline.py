"""Execution Pipeline collector — connectivity, rejections, signal freshness."""

from __future__ import annotations

import json
import time
from datetime import datetime
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

        Checks: connection_status.json, feed_health.json, last_order_attempt.json
        Each present+fresh file adds confidence. Missing files reduce it.
        """
        now = time.time()
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        state_files = [
            state_dir / "connection_status.json",
            state_dir / "feed_health.json",
            state_dir / "last_order_attempt.json",
        ]

        available = 0
        fresh = 0  # fresh = updated within 30 min
        total = len(state_files)

        for sf in state_files:
            if sf.exists():
                available += 1
                try:
                    age_h = (now - sf.stat().st_mtime) / 3600.0
                    if age_h < 0.5:
                        fresh += 1
                except OSError:
                    pass

        if available == 0:
            warnings.append("no connectivity data — confidence very low")
            return 0.1

        # All present and fresh → 1.0; all present but stale → 0.6; partial → scaled
        freshness_ratio = fresh / total
        availability_ratio = available / total

        confidence = 0.3 * availability_ratio + 0.7 * freshness_ratio
        return round(max(0.1, min(1.0, confidence)), 2)

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
        """Score based on specific connectivity state files, not generic mtime.

        Checks:
        - connection_status.json → MetaApi connection state
        - feed_health.json → per-symbol feed freshness
        - last_order_attempt.json → recent order execution results
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        if not state_dir.is_dir():
            return 0.0, 0.0

        score = 0.0
        checks = 0
        total_checks = 3

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
                        for sym, info in symbols.items():
                            if not isinstance(info, dict):
                                continue
                            total_symbols += 1
                            last_tick = info.get("last_tick", info.get("updated_at", 0))
                            if isinstance(last_tick, str):
                                try:
                                    last_tick = datetime.fromisoformat(last_tick).timestamp()
                                except (ValueError, TypeError):
                                    last_tick = 0
                            if now - last_tick > 300:  # stale if >5 min
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
                    result = data.get("result", data.get("status", "")).lower()
                    if result in ("success", "filled", "executed"):
                        score += 30.0
                    elif result in ("partial", "pending"):
                        score += 15.0
                    # else: failed or rejected → 0 contribution
                    checks += 1
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: if no specific state files, fall back to generic mtime check
        if checks == 0:
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

        return score, float(checks)
