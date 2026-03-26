"""Progress Scorer — multi-dimensional system assessment engine.

Runs all collectors in sequence, computes a weighted overall score,
detects trends against rolling history, and persists results for
cross-session tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.collectors.performance import PerformanceCollector
from novatrade.autonomy.collectors.pipeline import PipelineCollector
from novatrade.autonomy.collectors.risk_engine import RiskCollector
from novatrade.autonomy.collectors.strategy import StrategyCollector
from novatrade.autonomy.collectors.system_health import SystemHealthCollector
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    ScoringConfig,
)

log = logging.getLogger("novatrade.autonomy.progress_scorer")


class ProgressScorer:
    """Orchestrates metric collection, scoring, trend detection, and persistence."""

    def __init__(
        self,
        config: ScoringConfig | None = None,
        base_path: str = "/home/nova/nova-core",
    ) -> None:
        self.config = config or ScoringConfig()
        self.base_path = base_path
        self._history_path = Path(base_path) / "STATE" / "progress_history.json"
        self._write_lock = asyncio.Lock()
        self._collectors: dict[str, BaseCollector] = {
            "system_health": SystemHealthCollector(base_path),
            "execution_pipeline": PipelineCollector(base_path),
            "strategy_validity": StrategyCollector(base_path),
            "risk_engine": RiskCollector(base_path),
            "performance_stability": PerformanceCollector(base_path),
        }

    async def score(self) -> ProgressReport:
        """Run all collectors and produce a ProgressReport."""
        dimensions: dict[str, DimensionScore] = {}
        warnings: list[str] = []

        for name, collector in self._collectors.items():
            try:
                dim_score = await collector.collect()
                dimensions[name] = dim_score
            except Exception as exc:
                log.error("Collector %s failed: %s", name, exc)
                warnings.append(f"Collector {name} failed: {exc}")
                dimensions[name] = DimensionScore(name=name, score=0.0, warnings=[str(exc)])

        # Weighted average
        total_weight = sum(self.config.weights.get(k, 0.2) for k in dimensions)
        if total_weight == 0:
            total_weight = 1.0
        overall = sum(dimensions[k].score * self.config.weights.get(k, 0.2) for k in dimensions) / total_weight

        # Trend detection
        trends = self._compute_trends(dimensions)

        report = ProgressReport(
            overall_score=round(overall, 1),
            dimensions=dimensions,
            trends=trends,
            warnings=warnings,
        )

        # Persist to history
        await self._append_history(report)

        return report

    def _compute_trends(self, current: dict[str, DimensionScore]) -> dict[str, ScoreTrend]:
        """Compare current scores against rolling averages from history."""
        history = self._load_history()
        now = datetime.now(timezone.utc)
        trends: dict[str, ScoreTrend] = {}

        for name, dim in current.items():
            past_scores = [
                (
                    entry.get("generated_at", ""),
                    entry.get("dimensions", {}).get(name, {}).get("score", 0),
                )
                for entry in history
            ]

            avg_1h = self._avg_in_window(past_scores, now, hours=1)
            avg_6h = self._avg_in_window(past_scores, now, hours=6)
            avg_24h = self._avg_in_window(past_scores, now, hours=24)

            # Determine direction
            ref = avg_6h if avg_6h is not None else avg_24h
            if ref is not None:
                delta = dim.score - ref
                if delta > 5:
                    direction = "improving"
                elif delta < -5:
                    direction = "degrading"
                else:
                    direction = "stable"
            else:
                direction = "stable"

            trends[name] = ScoreTrend(
                current=dim.score,
                avg_1h=avg_1h,
                avg_6h=avg_6h,
                avg_24h=avg_24h,
                direction=direction,
            )

        return trends

    @staticmethod
    def _avg_in_window(scores: list[tuple[str, float]], now: datetime, hours: int) -> float | None:
        """Average scores within *hours* of *now*."""
        cutoff = now - timedelta(hours=hours)
        # Strip tzinfo for comparison if cutoff is aware but entry_dt is naive
        cutoff_naive = cutoff.replace(tzinfo=None)
        window: list[float] = []
        for ts, s in scores:
            if not isinstance(ts, str) or not ts:
                continue
            try:
                entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            # Compare in naive space — history entries may be naive or aware
            cmp_dt = entry_dt.replace(tzinfo=None)
            if cmp_dt >= cutoff_naive:
                window.append(s)
        if not window:
            return None
        return round(sum(window) / len(window), 1)

    def _load_history(self) -> list[dict]:
        """Load persisted history from disk."""
        if not self._history_path.exists():
            return []
        try:
            data = json.loads(self._history_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    async def _append_history(self, report: ProgressReport) -> None:
        """Append *report* to history, pruning entries older than retention.

        Uses an asyncio lock to serialise concurrent writes and writes
        atomically via a temp file + os.replace().
        """
        async with self._write_lock:
            history = self._load_history()
            history.append(json.loads(report.model_dump_json()))

            # Prune old entries
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.history_retention_days)
            cutoff_naive = cutoff.replace(tzinfo=None)
            pruned: list[dict] = []
            for h in history:
                ts_str = h.get("generated_at", "")
                if not ts_str:
                    continue
                try:
                    entry_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    if entry_dt >= cutoff_naive:
                        pruned.append(h)
                except (ValueError, TypeError):
                    # Keep entries with unparseable timestamps
                    pruned.append(h)

            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, then os.replace()
            fd, tmp_path = tempfile.mkstemp(dir=str(self._history_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(pruned, f, indent=2, default=str)
                os.replace(tmp_path, str(self._history_path))
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
