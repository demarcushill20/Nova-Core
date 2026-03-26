"""Execution Pipeline collector — connectivity, rejections, signal freshness."""

from __future__ import annotations

import json
import time
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

        return DimensionScore(
            name="Execution Pipeline",
            score=round(avg, 1),
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

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
        """Score based on STATE/novatrade/ price feed state freshness."""
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        if not state_dir.is_dir():
            return 0.0, 0.0

        # Look for any state file with recent modification
        newest_mtime = 0.0
        file_count = 0
        for p in state_dir.iterdir():
            if not p.is_file():
                continue
            file_count += 1
            try:
                mt = p.stat().st_mtime
                if mt > newest_mtime:
                    newest_mtime = mt
            except OSError:
                continue

        if file_count == 0:
            return 0.0, 0.0

        if newest_mtime == 0.0:
            return 0.0, 0.0

        age_h = (time.time() - newest_mtime) / 3600.0

        if age_h < 1:
            score = 100.0
        elif age_h < 6:
            score = 50.0
        else:
            score = 0.0

        return score, round(age_h, 2)
