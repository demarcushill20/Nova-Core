"""Outcome Analyzer — tracks decision effectiveness via before/after score deltas.

Records the system state at the time each decision is made, then measures
whether subsequent scoring cycles show improvement.  This closes the
feedback loop: decide → act → measure outcome → learn.

Key capabilities:
  - Record decision snapshots (scores at decision time)
  - Link decisions to their outcomes (score delta after N minutes)
  - Compute decision effectiveness (did REPAIR actually fix it?)
  - Surface patterns: which decision modes are most/least effective
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.decision_engine import Decision
from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.outcome_analyzer")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DecisionSnapshot:
    """Snapshot of system state when a decision was made."""

    decision_id: str  # unique id: "{mode}_{dimension}_{timestamp}"
    mode: str
    target_dimension: str | None
    reason: str
    decided_at: str  # ISO 8601
    scores_before: dict[str, float]  # dimension → score at decision time
    overall_before: float

    # Filled in later by resolve_outcome()
    scores_after: dict[str, float] | None = None
    overall_after: float | None = None
    resolved_at: str | None = None
    effectiveness: str | None = None  # "effective", "neutral", "counterproductive"
    delta: float | None = None  # overall score change


@dataclass
class EffectivenessReport:
    """Aggregated effectiveness statistics."""

    total_decisions: int = 0
    resolved: int = 0
    effective: int = 0
    neutral: int = 0
    counterproductive: int = 0
    by_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    avg_delta_by_mode: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class OutcomeAnalyzer:
    """Tracks decision→outcome linkage and computes effectiveness.

    Persistence: STATE/decision_outcomes.json (append-only, atomic writes).
    """

    # How long to wait before measuring outcome (minutes)
    OUTCOME_WINDOW_MINUTES = 30
    # Score delta thresholds
    EFFECTIVE_THRESHOLD = 3.0  # +3 points = decision was effective
    COUNTER_THRESHOLD = -3.0  # -3 points = counterproductive

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self._state_dir = Path(base_path) / "STATE"
        self._outcomes_path = self._state_dir / "decision_outcomes.json"

    def record_decision(self, decision: Decision, report: ProgressReport) -> DecisionSnapshot:
        """Create a snapshot linking a decision to current scores."""
        ts = decision.decided_at.isoformat() if decision.decided_at else datetime.now(timezone.utc).isoformat()
        decision_id = f"{decision.mode.value}_{decision.target_dimension or 'system'}_{ts[:19].replace(':', '')}"

        scores_before = {name: dim.score for name, dim in report.dimensions.items()}

        snapshot = DecisionSnapshot(
            decision_id=decision_id,
            mode=decision.mode.value,
            target_dimension=decision.target_dimension,
            reason=decision.reason[:200],
            decided_at=ts,
            scores_before=scores_before,
            overall_before=report.overall_score,
        )

        # Persist
        snapshots = self._load_snapshots()
        snapshots.append(snapshot)
        # Keep last 200 snapshots
        snapshots = snapshots[-200:]
        self._save_snapshots(snapshots)

        log.info(
            "Recorded decision snapshot: %s (overall=%.1f)",
            decision_id,
            report.overall_score,
        )
        return snapshot

    def resolve_pending(self, report: ProgressReport) -> list[DecisionSnapshot]:
        """Check for unresolved snapshots that are past the outcome window and resolve them."""
        snapshots = self._load_snapshots()
        now = datetime.now(timezone.utc)
        resolved: list[DecisionSnapshot] = []

        for snap in snapshots:
            if snap.resolved_at is not None:
                continue  # already resolved

            # Parse decided_at
            try:
                decided_dt = datetime.fromisoformat(snap.decided_at.replace("Z", "+00:00"))
                if decided_dt.tzinfo is None:
                    decided_dt = decided_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            elapsed = (now - decided_dt).total_seconds() / 60.0
            if elapsed < self.OUTCOME_WINDOW_MINUTES:
                continue

            # Resolve: compare current scores to decision-time scores
            scores_after = {name: dim.score for name, dim in report.dimensions.items()}
            snap.scores_after = scores_after
            snap.overall_after = report.overall_score
            snap.resolved_at = now.isoformat()
            snap.delta = report.overall_score - snap.overall_before

            # Classify effectiveness
            if (
                snap.target_dimension
                and snap.target_dimension in scores_after
                and snap.target_dimension in snap.scores_before
            ):
                dim_delta = scores_after[snap.target_dimension] - snap.scores_before[snap.target_dimension]
            else:
                dim_delta = snap.delta

            if dim_delta >= self.EFFECTIVE_THRESHOLD:
                snap.effectiveness = "effective"
            elif dim_delta <= self.COUNTER_THRESHOLD:
                snap.effectiveness = "counterproductive"
            else:
                snap.effectiveness = "neutral"

            resolved.append(snap)
            log.info(
                "Resolved outcome: %s — %s (delta=%.1f, target_delta=%.1f)",
                snap.decision_id,
                snap.effectiveness,
                snap.delta,
                dim_delta,
            )

        if resolved:
            self._save_snapshots(snapshots)

        return resolved

    def get_effectiveness_report(self) -> EffectivenessReport:
        """Compute aggregated effectiveness statistics."""
        snapshots = self._load_snapshots()
        report = EffectivenessReport()
        report.total_decisions = len(snapshots)

        mode_deltas: dict[str, list[float]] = {}
        mode_counts: dict[str, dict[str, int]] = {}

        for snap in snapshots:
            if snap.resolved_at is None:
                continue
            report.resolved += 1

            if snap.effectiveness == "effective":
                report.effective += 1
            elif snap.effectiveness == "counterproductive":
                report.counterproductive += 1
            else:
                report.neutral += 1

            # By mode
            mode = snap.mode
            if mode not in mode_counts:
                mode_counts[mode] = {"effective": 0, "neutral": 0, "counterproductive": 0}
            if snap.effectiveness:
                mode_counts[mode][snap.effectiveness] = mode_counts[mode].get(snap.effectiveness, 0) + 1

            if snap.delta is not None:
                mode_deltas.setdefault(mode, []).append(snap.delta)

        report.by_mode = mode_counts
        for mode, deltas in mode_deltas.items():
            report.avg_delta_by_mode[mode] = sum(deltas) / len(deltas) if deltas else 0.0

        return report

    def get_recent_outcomes(self, n: int = 10) -> list[DecisionSnapshot]:
        """Return the N most recent resolved outcomes."""
        snapshots = self._load_snapshots()
        resolved = [s for s in snapshots if s.resolved_at is not None]
        return resolved[-n:]

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _load_snapshots(self) -> list[DecisionSnapshot]:
        """Load snapshots from disk."""
        if not self._outcomes_path.exists():
            return []
        try:
            data = json.loads(self._outcomes_path.read_text())
            if not isinstance(data, list):
                return []
            return [DecisionSnapshot(**item) for item in data]
        except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
            log.warning("Failed to load decision outcomes: %s", exc)
            return []

    def _save_snapshots(self, snapshots: list[DecisionSnapshot]) -> None:
        """Atomic write snapshots to disk."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = [asdict(s) for s in snapshots]
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._state_dir),
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp_path, str(self._outcomes_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
