"""Tests for the autonomy dashboard CLI (`python3 -m novatrade.autonomy`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE_PATH = "/home/nova/nova-core"


class TestSnapshotMode:
    """Test --snapshot mode which reads STATE/autonomy_snapshot.json."""

    def test_snapshot_reads_existing_file(self, tmp_path):
        snap = {
            "overall_score": 75.0,
            "alert_level": "GREEN",
            "dimensions": {
                "system_health": {"score": 100.0, "trend": "stable"},
                "risk_engine": {"score": 50.0, "trend": "degrading"},
            },
            "decision": {"mode": "plan", "reason": "test reason", "target": "risk_engine"},
            "goal_tree": {"completed": 10, "total": 17, "actionable": ["Fix risk"]},
            "generated_at": "2026-03-26T12:00:00+00:00",
        }
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "autonomy_snapshot.json").write_text(json.dumps(snap))

        import novatrade.autonomy.__main__ as cli_mod
        from novatrade.autonomy.__main__ import (
            print_snapshot_mode,
        )

        old_state = cli_mod.STATE_DIR
        cli_mod.STATE_DIR = state_dir
        try:
            # Should not raise
            print_snapshot_mode()
        finally:
            cli_mod.STATE_DIR = old_state

    def test_snapshot_handles_missing_file(self, tmp_path, capsys):
        import novatrade.autonomy.__main__ as cli_mod

        old_state = cli_mod.STATE_DIR
        cli_mod.STATE_DIR = tmp_path / "nonexistent"
        try:
            cli_mod.print_snapshot_mode()
            out = capsys.readouterr().out
            assert "No snapshot found" in out
        finally:
            cli_mod.STATE_DIR = old_state


class TestFormattingHelpers:
    """Test the text formatting functions."""

    def test_alert_badge(self):
        from novatrade.autonomy.__main__ import _alert_badge

        assert _alert_badge(90) == "GREEN"
        assert _alert_badge(50) == "YELLOW"
        assert _alert_badge(20) == "RED"
        assert _alert_badge(70) == "GREEN"
        assert _alert_badge(40) == "YELLOW"
        assert _alert_badge(39.9) == "RED"

    def test_bar(self):
        from novatrade.autonomy.__main__ import _bar

        bar_100 = _bar(100.0)
        assert bar_100 == "[====================]"
        bar_0 = _bar(0.0)
        assert bar_0 == "[                    ]"
        bar_50 = _bar(50.0)
        assert "=" in bar_50
        assert " " in bar_50

    def test_trend_arrow(self):
        from novatrade.autonomy.__main__ import _trend_arrow

        assert _trend_arrow("improving") == "^"
        assert _trend_arrow("degrading") == "v"
        assert _trend_arrow("stable") == "="
        assert _trend_arrow("unknown") == "?"


class TestPrintScores:
    """Test score printing with real Pydantic models."""

    def test_print_scores_renders(self, capsys):
        from novatrade.autonomy.__main__ import print_scores
        from novatrade.autonomy.schemas import (
            DimensionScore,
            ProgressReport,
            ScoreTrend,
            SubMetric,
        )

        report = ProgressReport(
            overall_score=80.0,
            dimensions={
                "system_health": DimensionScore(
                    name="System Health",
                    score=100.0,
                    sub_metrics=[SubMetric(name="uptime", value=100.0)],
                ),
                "risk_engine": DimensionScore(
                    name="Risk Engine",
                    score=60.0,
                ),
            },
            trends={
                "system_health": ScoreTrend(current=100.0, direction="stable"),
                "risk_engine": ScoreTrend(current=60.0, direction="degrading"),
            },
        )

        print_scores(report)
        out = capsys.readouterr().out

        assert "80.0/100" in out
        assert "system_health" in out
        assert "risk_engine" in out
        assert "GREEN" in out
        assert "uptime=100" in out

    def test_print_scores_with_warnings(self, capsys):
        from novatrade.autonomy.__main__ import print_scores
        from novatrade.autonomy.schemas import DimensionScore, ProgressReport

        report = ProgressReport(
            overall_score=30.0,
            dimensions={
                "test_dim": DimensionScore(name="Test", score=30.0),
            },
            warnings=["Collector X failed"],
        )

        print_scores(report)
        out = capsys.readouterr().out
        assert "Collector X failed" in out


class TestPrintDecision:
    """Test decision rendering."""

    def test_print_decision_renders(self, capsys):
        from novatrade.autonomy.__main__ import print_decision
        from novatrade.autonomy.decision_engine import ActionMode, Decision

        decision = Decision(
            mode=ActionMode.REPAIR,
            reason="Risk engine regressed to 45",
            target_dimension="risk_engine",
            suggested_actions=["Check drawdown limits", "Verify halt state"],
            confidence="high",
        )

        print_decision(decision)
        out = capsys.readouterr().out

        assert "REPAIR" in out
        assert "risk_engine" in out
        assert "Check drawdown limits" in out


class TestCLIEntrypoint:
    """Integration tests for the CLI entrypoint."""

    def test_help_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "novatrade.autonomy", "--help"],
            capture_output=True,
            text=True,
            cwd=BASE_PATH,
        )
        assert result.returncode == 0
        assert "NovaCore Autonomy Dashboard" in result.stdout

    def test_snapshot_mode_runs(self):
        result = subprocess.run(
            [sys.executable, "-m", "novatrade.autonomy", "--snapshot"],
            capture_output=True,
            text=True,
            cwd=BASE_PATH,
            timeout=10,
        )
        assert result.returncode == 0

    def test_no_persist_on_dashboard(self):
        """Dashboard should not add entries to decision_history.json."""
        history_path = Path(BASE_PATH) / "STATE" / "decision_history.json"
        if history_path.exists():
            before = json.loads(history_path.read_text())
            before_count = len(before) if isinstance(before, list) else 0
        else:
            before_count = 0

        result = subprocess.run(
            [sys.executable, "-m", "novatrade.autonomy", "--scores"],
            capture_output=True,
            text=True,
            cwd=BASE_PATH,
            timeout=30,
        )
        assert result.returncode == 0

        if history_path.exists():
            after = json.loads(history_path.read_text())
            after_count = len(after) if isinstance(after, list) else 0
        else:
            after_count = 0

        assert after_count == before_count, "Dashboard should not persist decisions"
