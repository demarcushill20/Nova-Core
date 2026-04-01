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
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    ScoringConfig,
)

log = logging.getLogger("novatrade.autonomy.progress_scorer")


class ProgressScorer:
    """Orchestrates metric collection, scoring, trend detection, and persistence."""

    # Cap history entries to prevent unbounded file growth.
    # At ~44 entries/hour, 1500 gives ~34h of data — enough for 24h trends.
    MAX_HISTORY_ENTRIES: int = 1500

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

    # Per-collector timeout — prevents a hung collector from blocking the
    # entire autonomy cycle (e.g. systemctl stuck waiting for dbus).
    COLLECTOR_TIMEOUT_S: float = 10.0

    async def score(self) -> ProgressReport:
        """Run all collectors and produce a ProgressReport."""
        dimensions: dict[str, DimensionScore] = {}
        warnings: list[str] = []

        # Load history once — reused for cache-fallback and trend detection.
        history = self._load_history()
        cached_scores = self._last_cached_scores(history)

        for name, collector in self._collectors.items():
            try:
                dim_score = await asyncio.wait_for(
                    collector.collect(),
                    timeout=self.COLLECTOR_TIMEOUT_S,
                )
                dimensions[name] = dim_score
            except asyncio.TimeoutError:
                log.error("Collector %s timed out after %.0fs", name, self.COLLECTOR_TIMEOUT_S)
                fallback = cached_scores.get(name)
                if fallback is not None:
                    warnings.append(f"Collector {name} timed out — using cached score {fallback:.1f}")
                    dimensions[name] = DimensionScore(
                        name=name,
                        score=fallback,
                        confidence=0.3,  # stale data — low confidence
                        warnings=["Timed out — cached score from last successful run"],
                    )
                else:
                    warnings.append(f"Collector {name} timed out — no cached score, defaulting to 0")
                    dimensions[name] = DimensionScore(
                        name=name,
                        score=0.0,
                        confidence=0.1,
                        warnings=["Timed out, no cache"],
                    )
            except Exception as exc:
                log.error("Collector %s failed: %s", name, exc)
                fallback = cached_scores.get(name)
                if fallback is not None:
                    warnings.append(f"Collector {name} failed — using cached score {fallback:.1f}")
                    dimensions[name] = DimensionScore(
                        name=name,
                        score=fallback,
                        confidence=0.3,  # stale data — low confidence
                        warnings=[f"Failed ({exc}) — cached score from last successful run"],
                    )
                else:
                    warnings.append(f"Collector {name} failed: {exc}")
                    dimensions[name] = DimensionScore(name=name, score=0.0, confidence=0.1, warnings=[str(exc)])

        # Confidence-adjusted weighted average: low-confidence dimensions
        # contribute min(score, 50) to prevent inflation from unreliable data.
        total_weight = 0.0
        weighted_sum = 0.0
        for k, dim in dimensions.items():
            w = self.config.weights.get(k, 0.2)
            # Low-confidence dimensions: use min(score, 50) to prevent inflation
            effective_score = dim.score if dim.confidence >= 0.5 else min(dim.score, 50.0)
            weighted_sum += effective_score * w
            total_weight += w
        if total_weight == 0:
            total_weight = 1.0
        overall = weighted_sum / total_weight

        # Mission guardrail: cap overall if any mission-critical dimension is RED
        for critical_dim in self.config.mission_critical:
            dim_check = dimensions.get(critical_dim)
            if dim_check and dim_check.alert_level == AlertLevel.RED and overall > self.config.red_cap_overall:
                warnings.append(
                    f"Overall capped at {self.config.red_cap_overall} — {critical_dim} is RED ({dim_check.score:.0f})"
                )
                overall = self.config.red_cap_overall
                break

        # Confidence penalty: low-confidence dimensions can only drag down, not inflate
        for name, dim in dimensions.items():
            if dim.confidence < 0.5:
                warnings.append(
                    f"{name} has low confidence ({dim.confidence:.1f}) — score may not reflect actual state"
                )

        # Trend detection (reuses pre-loaded history)
        trends = self._compute_trends(dimensions, history)

        report = ProgressReport(
            overall_score=round(overall, 1),
            dimensions=dimensions,
            trends=trends,
            warnings=warnings,
        )

        # Persist to history
        await self._append_history(report)

        return report

    def _last_cached_scores(self, history: list[dict] | None = None) -> dict[str, float]:
        """Return the most recent score per dimension from persisted history.

        Used as a fallback when a collector throws or times out — prevents a
        single failing collector from zeroing its dimension and triggering
        false REPAIR/RESEARCH decisions.
        """
        if history is None:
            history = self._load_history()
        if not history:
            return {}
        # Walk backwards to find the most recent entry with dimension data.
        for entry in reversed(history):
            dims = entry.get("dimensions", {})
            if dims:
                return {name: max(0.0, min(100.0, d.get("score", 0.0))) for name, d in dims.items()}
        return {}

    def _compute_trends(
        self,
        current: dict[str, DimensionScore],
        history: list[dict] | None = None,
    ) -> dict[str, ScoreTrend]:
        """Compare current scores against rolling averages from history."""
        if history is None:
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

            # Cap total entries to prevent unbounded file growth
            if len(pruned) > self.MAX_HISTORY_ENTRIES:
                pruned = pruned[-self.MAX_HISTORY_ENTRIES :]

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
