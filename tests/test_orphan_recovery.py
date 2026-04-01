"""Tests for watcher orphan recovery and self-heal investigator orphan diagnosis."""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# ---- Watcher orphan recovery ----


class TestWatcherOrphanRecovery:
    """Test the watcher's recover_orphaned_tasks() function."""

    def test_recovers_orphaned_inprogress(self, tmp_path):
        """Orphaned .inprogress files are reset to .md for retry."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_test.md.inprogress"
        ip.write_text("task content")
        # Backdate to make it orphaned (>15 min)
        old_ts = time.time() - 1800  # 30 min ago
        os.utime(ip, (old_ts, old_ts))

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.increment_retry", return_value=None),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
        ):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 1
        assert not ip.exists()
        assert (tasks_dir / "0001_test.md").exists()

    def test_skips_fresh_inprogress(self, tmp_path):
        """Recent .inprogress files are not touched."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_fresh.md.inprogress"
        ip.write_text("task content")
        # Leave mtime as now — not orphaned

        with patch("watcher.TASKS_DIR", tasks_dir), patch("watcher._last_orphan_recovery_time", 0):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 0
        assert ip.exists()

    def test_marks_failed_when_max_retries(self, tmp_path):
        """Orphaned tasks at max retries are marked as .failed."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()  # Empty — isolates from real OUTPUT

        ip = tasks_dir / "0001_maxed.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        class FakeCheckpoint:
            retry_count = 3

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.OUTPUT_DIR", output_dir),
            patch("watcher.increment_retry", return_value=FakeCheckpoint()),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
        ):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 1
        assert not ip.exists()
        assert (tasks_dir / "0001_maxed.md.failed").exists()

    def test_respects_interval_throttle(self, tmp_path):
        """Recovery is throttled to ORPHAN_RECOVERY_INTERVAL."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_throttled.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        import watcher

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.increment_retry", return_value=None),
            patch("watcher.MAX_TASK_RETRIES", 3),
        ):
            # Set last recovery to now — should throttle
            watcher._last_orphan_recovery_time = time.time()
            result = watcher.recover_orphaned_tasks(force=False)

        assert len(result) == 0  # Throttled
        assert ip.exists()  # Not touched

    def test_startup_grace_period_blocks_recovery(self, tmp_path):
        """Orphan recovery is skipped during startup grace period."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_grace.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        import watcher

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.increment_retry", return_value=None),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
            # Simulate recent boot — grace period active
            patch("watcher._watcher_boot_time", time.time()),
        ):
            result = watcher.recover_orphaned_tasks(force=False)

        assert len(result) == 0  # Blocked by grace period
        assert ip.exists()  # Not touched

    def test_startup_grace_period_bypassed_by_force(self, tmp_path):
        """force=True bypasses the startup grace period."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_forced.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.increment_retry", return_value=None),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
            # Simulate recent boot — grace period active
            patch("watcher._watcher_boot_time", time.time()),
        ):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 1
        assert not ip.exists()
        assert (tasks_dir / "0001_forced.md").exists()

    def test_rescues_orphan_with_output(self, tmp_path):
        """Orphaned task with existing OUTPUT is marked .done, not .failed."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        stem = "0001_rescued"
        ip = tasks_dir / f"{stem}.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        # Create a matching OUTPUT file
        output_file = output_dir / f"{stem}__20260331-120000.md"
        output_file.write_text("## CONTRACT\nsummary: done\n")

        class FakeCheckpoint:
            retry_count = 3

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.OUTPUT_DIR", output_dir),
            patch("watcher.increment_retry", return_value=FakeCheckpoint()),
            patch("watcher.clear_checkpoint"),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
        ):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 1
        assert not ip.exists()
        # Should be .done, not .failed — because OUTPUT exists
        assert (tasks_dir / f"{stem}.md.done").exists()
        assert not (tasks_dir / f"{stem}.md.failed").exists()

    def test_marks_failed_when_no_output(self, tmp_path):
        """Orphaned task at max retries with NO output is still marked .failed."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()  # Empty — no matching files

        stem = "0001_nooutput"
        ip = tasks_dir / f"{stem}.md.inprogress"
        ip.write_text("task content")
        old_ts = time.time() - 1800
        os.utime(ip, (old_ts, old_ts))

        class FakeCheckpoint:
            retry_count = 3

        with (
            patch("watcher.TASKS_DIR", tasks_dir),
            patch("watcher.OUTPUT_DIR", output_dir),
            patch("watcher.increment_retry", return_value=FakeCheckpoint()),
            patch("watcher.MAX_TASK_RETRIES", 3),
            patch("watcher._last_orphan_recovery_time", 0),
        ):
            from watcher import recover_orphaned_tasks

            result = recover_orphaned_tasks(force=True)

        assert len(result) == 1
        assert not ip.exists()
        assert (tasks_dir / f"{stem}.md.failed").exists()


# ---- OUTPUT stem matching ----


class TestOutputStemMatching:
    """Test _output_matches_stem prevents false matches on similar stems."""

    def test_exact_stem_with_timestamp(self):
        """Standard output filename matches its stem."""
        from watcher import _output_matches_stem

        assert _output_matches_stem("shift_1_health__20260331-120000", "shift_1_health")

    def test_stem_with_sub_workflow(self):
        """Sub-workflow output matches parent stem."""
        from watcher import _output_matches_stem

        assert _output_matches_stem("shift_1_health_inspect__20260331-120000", "shift_1_health")

    def test_rejects_numeric_prefix_collision(self):
        """stem 'shift_1' must NOT match 'shift_10_foo'."""
        from watcher import _output_matches_stem

        assert not _output_matches_stem("shift_10_foo__20260331-120000", "shift_1")

    def test_rejects_partial_word_match(self):
        """stem 'deploy' must NOT match 'deployment_fix'."""
        from watcher import _output_matches_stem

        assert not _output_matches_stem("deployment_fix__20260331-120000", "deploy")

    def test_exact_stem_name(self):
        """Stem matching the entire filename (no timestamp) works."""
        from watcher import _output_matches_stem

        assert _output_matches_stem("task_done", "task_done")

    def test_stem_with_retry_suffix(self):
        """Retry outputs match their stem."""
        from watcher import _output_matches_stem

        assert _output_matches_stem("task_abc__retry1__20260331-120000", "task_abc")

    def test_find_any_output_no_false_match(self, tmp_path):
        """_find_any_output doesn't return outputs from a different stem."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        # Create output for shift_10, NOT shift_1
        (output_dir / "shift_10_foo__20260331-120000.md").write_text("content")

        with patch("watcher.OUTPUT_DIR", output_dir):
            from watcher import _find_any_output

            result = _find_any_output("shift_1")

        assert result is None  # Must NOT match shift_10_foo

    def test_find_any_output_matches_correct_stem(self, tmp_path):
        """_find_any_output returns output for the correct stem."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        target = output_dir / "shift_1_health__20260331-120000.md"
        target.write_text("content")
        # Also create a similar but different stem
        (output_dir / "shift_10_foo__20260331-120000.md").write_text("other")

        with patch("watcher.OUTPUT_DIR", output_dir):
            from watcher import _find_any_output

            result = _find_any_output("shift_1_health")

        assert result is not None
        assert result.name == "shift_1_health__20260331-120000.md"


# ---- Self-heal investigator orphan diagnosis ----


class TestSelfHealOrphanDiagnosis:
    """Test the self-heal investigator's orphan diagnosis pattern."""

    def test_diagnoses_orphaned_task(self):
        """Orphaned task message triggers auto-fix diagnosis."""
        from agents.self_heal_investigator import diagnose

        msg = (
            "Health check failed: task_queue — 22 pending (0 stale, 22 deferred), "
            "1 in-progress, ORPHANED: shift_20260328_12_novatrade_testing.md.inprogress"
        )
        result = diagnose(msg, "heartbeat", {})

        assert result.can_auto_fix is True
        assert result.fix_action == "recover_orphan"
        assert result.confidence == "high"
        assert "shift_20260328_12_novatrade_testing.md.inprogress" in result.fix_params.get("orphaned_names", [])

    def test_diagnoses_multiple_orphans(self):
        """Multiple orphaned tasks in one message are all captured."""
        from agents.self_heal_investigator import diagnose

        msg = "Health check failed: task_queue — ORPHANED: task_a.md.inprogress, task_b.md.inprogress"
        result = diagnose(msg, "heartbeat", {})

        assert result.can_auto_fix is True
        assert result.fix_action == "recover_orphan"
        names = result.fix_params.get("orphaned_names", [])
        assert len(names) >= 1  # At least one captured

    def test_non_orphan_heartbeat_not_matched(self):
        """Non-orphan heartbeat messages don't trigger orphan fix."""
        from agents.self_heal_investigator import diagnose

        msg = "Health check failed: disk — usage at 90%"
        result = diagnose(msg, "heartbeat", {})

        assert result.fix_action != "recover_orphan"

    def test_recover_orphan_fix_action_registered(self):
        """The recover_orphan fix action is in FIX_ACTIONS."""
        from agents.self_heal_investigator import FIX_ACTIONS

        assert "recover_orphan" in FIX_ACTIONS

    def test_execute_fix_calls_recovery(self, tmp_path):
        """execute_fix dispatches to orphan recovery handler."""
        from agents.self_heal_investigator import DiagnosisResult, execute_fix

        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        diag = DiagnosisResult(
            root_cause="orphaned tasks",
            can_auto_fix=True,
            fix_action="recover_orphan",
            fix_params={"orphaned_names": None},
            confidence="high",
        )

        with patch("agents.self_heal_investigator.TASKS_DIR", tasks_dir):
            success, detail = execute_fix(diag)

        assert success is True
        assert "no orphaned tasks" in detail


# ---- Heartbeat auto-recovery ----


class TestHeartbeatAutoRecovery:
    """Test heartbeat's inline orphan recovery."""

    def test_auto_recovers_orphan_in_check(self, tmp_path):
        """check_task_queue auto-recovers orphaned tasks inline."""
        import heartbeat

        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        ip = tasks_dir / "0001_stuck.md.inprogress"
        ip.write_text("stuck task")
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
        os.utime(ip, (old_ts, old_ts))

        with patch.object(heartbeat, "TASKS_DIR", tasks_dir):
            result = heartbeat.check_task_queue()

        assert result["ok"] is True
        assert "AUTO-RECOVERED" in result["detail"]
        assert not ip.exists()
        assert (tasks_dir / "0001_stuck.md").exists()
