"""Tests for OutcomeAnalyzer — decision→outcome tracking and effectiveness scoring."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.outcome_analyzer import (
    DecisionSnapshot,
    OutcomeAnalyzer,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    overall: float = 75.0,
    dims: dict[str, float] | None = None,
) -> ProgressReport:
    """Build a ProgressReport with given scores."""
    if dims is None:
        dims = {
            "system_health": 90.0,
            "strategy_validity": 60.0,
            "execution_pipeline": 80.0,
            "risk_engine": 85.0,
            "performance_stability": 70.0,
        }
    return ProgressReport(
        overall_score=overall,
        dimensions={name: DimensionScore(name=name, score=score) for name, score in dims.items()},
        trends={name: ScoreTrend(current=score) for name, score in dims.items()},
    )


def _make_decision(
    mode: ActionMode = ActionMode.REPAIR,
    target: str = "strategy_validity",
    reason: str = "Test decision",
) -> Decision:
    return Decision(
        mode=mode,
        reason=reason,
        target_dimension=target,
        confidence="high",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRecordDecision:
    """Test recording decision snapshots."""

    def test_record_creates_snapshot(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        decision = _make_decision()
        report = _make_report()

        snap = analyzer.record_decision(decision, report)

        assert snap.mode == "repair"
        assert snap.target_dimension == "strategy_validity"
        assert snap.overall_before == 75.0
        assert snap.scores_before["system_health"] == 90.0
        assert snap.resolved_at is None

    def test_record_persists_to_disk(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        analyzer.record_decision(_make_decision(), _make_report())

        # Check file exists
        outcomes_path = tmp_path / "STATE" / "decision_outcomes.json"
        assert outcomes_path.exists()

        data = json.loads(outcomes_path.read_text())
        assert len(data) == 1
        assert data[0]["mode"] == "repair"

    def test_record_multiple_snapshots(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))

        for i in range(5):
            analyzer.record_decision(
                _make_decision(reason=f"Decision {i}"),
                _make_report(overall=60.0 + i * 5),
            )

        snapshots = analyzer._load_snapshots()
        assert len(snapshots) == 5

    def test_record_caps_at_200(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))

        for i in range(210):
            analyzer.record_decision(
                _make_decision(reason=f"Decision {i}"),
                _make_report(),
            )

        snapshots = analyzer._load_snapshots()
        assert len(snapshots) == 200


class TestResolvePending:
    """Test resolving pending outcomes."""

    def test_resolve_skips_recent(self, tmp_path: Path) -> None:
        """Snapshots < OUTCOME_WINDOW_MINUTES old should not be resolved."""
        analyzer = OutcomeAnalyzer(str(tmp_path))
        analyzer.record_decision(_make_decision(), _make_report())

        resolved = analyzer.resolve_pending(_make_report(overall=80.0))
        assert len(resolved) == 0

    def test_resolve_past_window(self, tmp_path: Path) -> None:
        """Snapshots past the window should be resolved."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        # Create a snapshot with decided_at in the past
        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        snap = DecisionSnapshot(
            decision_id="test_old",
            mode="repair",
            target_dimension="strategy_validity",
            reason="Old decision",
            decided_at=old_time.isoformat(),
            scores_before={"strategy_validity": 50.0, "system_health": 90.0},
            overall_before=70.0,
        )
        analyzer._save_snapshots([snap])

        # Resolve with improved scores
        after_report = _make_report(
            overall=80.0,
            dims={"strategy_validity": 75.0, "system_health": 90.0},
        )
        resolved = analyzer.resolve_pending(after_report)

        assert len(resolved) == 1
        assert resolved[0].effectiveness == "effective"
        assert resolved[0].delta == 10.0
        assert resolved[0].overall_after == 80.0

    def test_resolve_counterproductive(self, tmp_path: Path) -> None:
        """Score decrease should be marked counterproductive."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        snap = DecisionSnapshot(
            decision_id="test_bad",
            mode="repair",
            target_dimension="strategy_validity",
            reason="Bad repair",
            decided_at=old_time.isoformat(),
            scores_before={"strategy_validity": 70.0},
            overall_before=80.0,
        )
        analyzer._save_snapshots([snap])

        after_report = _make_report(
            overall=72.0,
            dims={"strategy_validity": 55.0},
        )
        resolved = analyzer.resolve_pending(after_report)

        assert len(resolved) == 1
        assert resolved[0].effectiveness == "counterproductive"

    def test_resolve_neutral(self, tmp_path: Path) -> None:
        """Small score change should be marked neutral."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        snap = DecisionSnapshot(
            decision_id="test_neutral",
            mode="monitor",
            target_dimension="system_health",
            reason="Neutral",
            decided_at=old_time.isoformat(),
            scores_before={"system_health": 90.0},
            overall_before=80.0,
        )
        analyzer._save_snapshots([snap])

        after_report = _make_report(
            overall=81.0,
            dims={"system_health": 91.0},
        )
        resolved = analyzer.resolve_pending(after_report)

        assert len(resolved) == 1
        assert resolved[0].effectiveness == "neutral"

    def test_resolve_skips_already_resolved(self, tmp_path: Path) -> None:
        """Already resolved snapshots should be skipped."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        snap = DecisionSnapshot(
            decision_id="test_done",
            mode="repair",
            target_dimension="system_health",
            reason="Already done",
            decided_at=old_time.isoformat(),
            scores_before={"system_health": 80.0},
            overall_before=80.0,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            effectiveness="effective",
            delta=5.0,
        )
        analyzer._save_snapshots([snap])

        resolved = analyzer.resolve_pending(_make_report())
        assert len(resolved) == 0


class TestEffectivenessReport:
    """Test aggregated effectiveness reporting."""

    def test_empty_report(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        report = analyzer.get_effectiveness_report()
        assert report.total_decisions == 0
        assert report.resolved == 0

    def test_report_with_resolved(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))

        snapshots = [
            DecisionSnapshot(
                decision_id="s1",
                mode="repair",
                target_dimension="system_health",
                reason="r1",
                decided_at="2026-01-01T00:00:00+00:00",
                scores_before={"system_health": 50.0},
                overall_before=60.0,
                scores_after={"system_health": 80.0},
                overall_after=75.0,
                resolved_at="2026-01-01T00:30:00+00:00",
                effectiveness="effective",
                delta=15.0,
            ),
            DecisionSnapshot(
                decision_id="s2",
                mode="repair",
                target_dimension="system_health",
                reason="r2",
                decided_at="2026-01-01T01:00:00+00:00",
                scores_before={"system_health": 70.0},
                overall_before=75.0,
                scores_after={"system_health": 65.0},
                overall_after=72.0,
                resolved_at="2026-01-01T01:30:00+00:00",
                effectiveness="counterproductive",
                delta=-3.0,
            ),
            DecisionSnapshot(
                decision_id="s3",
                mode="monitor",
                target_dimension=None,
                reason="r3",
                decided_at="2026-01-01T02:00:00+00:00",
                scores_before={},
                overall_before=75.0,
                scores_after={},
                overall_after=76.0,
                resolved_at="2026-01-01T02:30:00+00:00",
                effectiveness="neutral",
                delta=1.0,
            ),
        ]
        analyzer._save_snapshots(snapshots)

        report = analyzer.get_effectiveness_report()
        assert report.total_decisions == 3
        assert report.resolved == 3
        assert report.effective == 1
        assert report.counterproductive == 1
        assert report.neutral == 1
        assert "repair" in report.by_mode
        assert report.avg_delta_by_mode["repair"] == 6.0  # (15 + -3) / 2


class TestRecentOutcomes:
    """Test recent outcomes retrieval."""

    def test_get_recent_empty(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        assert analyzer.get_recent_outcomes() == []

    def test_get_recent_n(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        snapshots = [
            DecisionSnapshot(
                decision_id=f"s{i}",
                mode="repair",
                target_dimension=None,
                reason=f"r{i}",
                decided_at=f"2026-01-01T{i:02d}:00:00+00:00",
                scores_before={},
                overall_before=70.0,
                resolved_at=f"2026-01-01T{i:02d}:30:00+00:00",
                effectiveness="effective",
                delta=5.0,
            )
            for i in range(5)
        ]
        analyzer._save_snapshots(snapshots)

        recent = analyzer.get_recent_outcomes(3)
        assert len(recent) == 3
        assert recent[-1].decision_id == "s4"


class TestPersistence:
    """Test persistence edge cases."""

    def test_load_empty_file(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "STATE" / "decision_outcomes.json").write_text("")

        assert analyzer._load_snapshots() == []

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "STATE" / "decision_outcomes.json").write_text("{bad json")

        assert analyzer._load_snapshots() == []

    def test_load_non_list(self, tmp_path: Path) -> None:
        analyzer = OutcomeAnalyzer(str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "STATE" / "decision_outcomes.json").write_text('{"key": "value"}')

        assert analyzer._load_snapshots() == []
