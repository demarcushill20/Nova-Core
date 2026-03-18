"""Tests for Phase 4.6: DRY Cycles + HeartbeatSnapshot.

Covers:
- HeartbeatSnapshot dataclass construction, serialization, defaults
- _append_metrics() JSONL writing and rotation
- get_trend_summary() trend analysis (availability, hotspots, regressions)
- _run_claude_cycle() unified runner (paused, active hours, cooldown, MPG, success, timeout, empty)
- Thin wrappers: _run_research_cycle and _run_planning_cycle delegation
"""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest import mock

# Add project root to path  # noqa: E402
_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import heartbeat  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tmp_base(tmp_path: Path) -> None:
    """Point heartbeat module paths at a temp directory."""
    heartbeat.BASE = tmp_path
    heartbeat.HEARTBEAT_FILE = tmp_path / "HEARTBEAT.md"
    heartbeat.STATE_DIR = tmp_path / "STATE"
    heartbeat.TASKS_DIR = tmp_path / "TASKS"
    heartbeat.OUTPUT_DIR = tmp_path / "OUTPUT"
    heartbeat.LOGS_DIR = tmp_path / "LOGS"
    heartbeat.METRICS_JSONL = heartbeat.STATE_DIR / "heartbeat_metrics.jsonl"
    for d in (
        heartbeat.STATE_DIR,
        heartbeat.TASKS_DIR,
        heartbeat.OUTPUT_DIR,
        heartbeat.LOGS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def _make_snapshot(
    ts: str = "2026-03-18T12:00:00Z",
    epoch: int = 1773926400,
    status: str = "healthy",
    total_checks: int = 5,
    passed: int = 5,
    failed: int = 0,
    failed_names: list[str] | None = None,
    duration_ms: float = 42.5,
    checks: dict | None = None,  # type: ignore[type-arg]
) -> heartbeat.HeartbeatSnapshot:
    """Create a HeartbeatSnapshot with sensible defaults."""
    return heartbeat.HeartbeatSnapshot(
        ts=ts,
        epoch=epoch,
        status=status,
        total_checks=total_checks,
        passed=passed,
        failed=failed,
        failed_names=failed_names if failed_names is not None else [],
        duration_ms=duration_ms,
        checks=checks if checks is not None else {},
    )


# ---------------------------------------------------------------------------
# HeartbeatSnapshot dataclass
# ---------------------------------------------------------------------------


class TestHeartbeatSnapshot:
    """HeartbeatSnapshot construction, defaults, and serialization."""

    def test_construction_with_all_fields(self):
        s = heartbeat.HeartbeatSnapshot(
            ts="2026-03-18T12:00:00Z",
            epoch=1773926400,
            status="healthy",
            total_checks=10,
            passed=9,
            failed=1,
            failed_names=["disk"],
            duration_ms=123.4,
            checks={"disk": {"ok": False, "detail": "90% full"}},
        )
        assert s.ts == "2026-03-18T12:00:00Z"
        assert s.epoch == 1773926400
        assert s.status == "healthy"
        assert s.total_checks == 10
        assert s.passed == 9
        assert s.failed == 1
        assert s.failed_names == ["disk"]
        assert s.duration_ms == 123.4
        assert s.checks["disk"]["ok"] is False

    def test_defaults(self):
        s = heartbeat.HeartbeatSnapshot(ts="T", epoch=0, status="healthy", total_checks=0, passed=0, failed=0)
        assert s.failed_names == []
        assert s.duration_ms == 0.0
        assert s.checks == {}

    def test_asdict_produces_valid_json(self):
        s = _make_snapshot(failed_names=["a", "b"])
        d = asdict(s)
        text = json.dumps(d)
        parsed = json.loads(text)
        assert parsed["failed_names"] == ["a", "b"]
        assert parsed["status"] == "healthy"

    def test_default_factory_isolation(self):
        """Ensure default_factory lists/dicts are independent across instances."""
        s1 = _make_snapshot()
        s2 = _make_snapshot()
        s1.failed_names.append("x")
        assert "x" not in s2.failed_names


# ---------------------------------------------------------------------------
# _append_metrics()
# ---------------------------------------------------------------------------


class TestAppendMetrics:
    """JSONL writing and rotation."""

    def test_writes_jsonl_line(self, tmp_path):
        _make_tmp_base(tmp_path)
        s = _make_snapshot()
        heartbeat._append_metrics(s)
        lines = heartbeat.METRICS_JSONL.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["status"] == "healthy"

    def test_appends_multiple(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(5):
            heartbeat._append_metrics(_make_snapshot(epoch=i))
        lines = heartbeat.METRICS_JSONL.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_rotation_keeps_newest(self, tmp_path):
        _make_tmp_base(tmp_path)
        # Temporarily lower the cap
        original_max = heartbeat.METRICS_MAX_ENTRIES
        try:
            heartbeat.METRICS_MAX_ENTRIES = 10
            for i in range(25):
                heartbeat._append_metrics(_make_snapshot(epoch=i))
            lines = heartbeat.METRICS_JSONL.read_text().strip().splitlines()
            assert len(lines) == 10
            # Newest entry should have epoch=24
            last = json.loads(lines[-1])
            assert last["epoch"] == 24
            # Oldest kept entry should have epoch=15
            first = json.loads(lines[0])
            assert first["epoch"] == 15
        finally:
            heartbeat.METRICS_MAX_ENTRIES = original_max

    def test_creates_state_dir_if_missing(self, tmp_path):
        _make_tmp_base(tmp_path)
        # Remove STATE dir
        import shutil

        shutil.rmtree(heartbeat.STATE_DIR)
        assert not heartbeat.STATE_DIR.exists()
        heartbeat._append_metrics(_make_snapshot())
        assert heartbeat.METRICS_JSONL.exists()


# ---------------------------------------------------------------------------
# get_trend_summary()
# ---------------------------------------------------------------------------


class TestGetTrendSummary:
    """Trend analysis: availability, hotspots, regressions."""

    def test_empty_file_returns_defaults(self, tmp_path):
        _make_tmp_base(tmp_path)
        result = heartbeat.get_trend_summary()
        assert result == {
            "availability_pct": 100.0,
            "total_snapshots": 0,
            "failure_hotspots": [],
            "recent_regressions": [],
        }

    def test_all_healthy(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(10):
            heartbeat._append_metrics(_make_snapshot(epoch=i))
        result = heartbeat.get_trend_summary()
        assert result["availability_pct"] == 100.0
        assert result["total_snapshots"] == 10
        assert result["failure_hotspots"] == []
        assert result["recent_regressions"] == []

    def test_mixed_availability(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(10):
            status = "healthy" if i % 2 == 0 else "unhealthy"
            names = ["disk"] if status == "unhealthy" else []
            heartbeat._append_metrics(_make_snapshot(epoch=i, status=status, failed_names=names, failed=len(names)))
        result = heartbeat.get_trend_summary()
        assert result["availability_pct"] == 50.0
        assert result["total_snapshots"] == 10

    def test_failure_hotspots_ranking(self, tmp_path):
        _make_tmp_base(tmp_path)
        # disk fails 5 times, backup fails 3 times, ruff fails 1 time
        for i in range(5):
            heartbeat._append_metrics(_make_snapshot(epoch=i, status="unhealthy", failed_names=["disk"], failed=1))
        for i in range(5, 8):
            heartbeat._append_metrics(_make_snapshot(epoch=i, status="unhealthy", failed_names=["backup"], failed=1))
        heartbeat._append_metrics(_make_snapshot(epoch=8, status="unhealthy", failed_names=["ruff"], failed=1))
        result = heartbeat.get_trend_summary()
        hotspots = result["failure_hotspots"]
        assert len(hotspots) == 3
        assert hotspots[0] == ("disk", 5)
        assert hotspots[1] == ("backup", 3)
        assert hotspots[2] == ("ruff", 1)

    def test_regression_detection(self, tmp_path):
        _make_tmp_base(tmp_path)
        # 10 healthy entries (prior window)
        for i in range(10):
            heartbeat._append_metrics(_make_snapshot(epoch=i))
        # 3 entries with a new failure "new_check"
        for i in range(10, 13):
            heartbeat._append_metrics(_make_snapshot(epoch=i, status="unhealthy", failed_names=["new_check"], failed=1))
        result = heartbeat.get_trend_summary()
        assert "new_check" in result["recent_regressions"]

    def test_no_regression_if_prior_also_failed(self, tmp_path):
        _make_tmp_base(tmp_path)
        # All entries fail "disk"
        for i in range(13):
            heartbeat._append_metrics(_make_snapshot(epoch=i, status="unhealthy", failed_names=["disk"], failed=1))
        result = heartbeat.get_trend_summary()
        # "disk" is NOT a regression since it also failed in the prior window
        assert "disk" not in result["recent_regressions"]

    def test_window_limits_entries(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(200):
            heartbeat._append_metrics(_make_snapshot(epoch=i))
        result = heartbeat.get_trend_summary(window=50)
        assert result["total_snapshots"] == 50

    def test_corrupt_jsonl_returns_defaults(self, tmp_path):
        _make_tmp_base(tmp_path)
        heartbeat.METRICS_JSONL.write_text("not valid json\n{also bad\n")
        result = heartbeat.get_trend_summary()
        assert result["total_snapshots"] == 0


# ---------------------------------------------------------------------------
# _run_claude_cycle() — unified runner
# ---------------------------------------------------------------------------


class TestRunClaudeCycle:
    """Unified LLM cycle runner behaviour."""

    def test_paused_skips_immediately(self, capsys):
        """When paused=True, nothing runs."""
        prompt_builder = mock.Mock()
        cooldown_check = mock.Mock()
        cooldown_update = mock.Mock()
        log_fn = mock.Mock()

        heartbeat._run_claude_cycle(
            cycle_name="test",
            prompt_builder=prompt_builder,
            timeout=60,
            cooldown_check=cooldown_check,
            cooldown_update=cooldown_update,
            log_fn=log_fn,
            emoji="T",
            label="Test",
            mpg_caller="test",
            paused=True,
        )
        prompt_builder.assert_not_called()
        cooldown_check.assert_not_called()
        captured = capsys.readouterr()
        assert "Paused, skipping" in captured.out

    def test_outside_active_hours_skips(self, capsys):
        """Outside active hours should skip without calling subprocess."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 3  # 3 AM
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 8),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 22),
            mock.patch("heartbeat.datetime") as mdt,
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(),
                timeout=60,
                cooldown_check=mock.Mock(),
                cooldown_update=mock.Mock(),
                log_fn=mock.Mock(),
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        captured = capsys.readouterr()
        assert "Outside active hours" in captured.out

    def test_cooldown_blocks(self, capsys):
        """When cooldown check returns False, cycle should skip."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(),
                timeout=60,
                cooldown_check=mock.Mock(return_value=False),
                cooldown_update=mock.Mock(),
                log_fn=mock.Mock(),
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        captured = capsys.readouterr()
        assert "Cooldown active" in captured.out

    def test_mpg_blocks(self, capsys):
        """When max-plan guard blocks, cycle should skip."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", return_value=(False, "budget exceeded")),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(),
                timeout=60,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=mock.Mock(),
                log_fn=mock.Mock(),
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        captured = capsys.readouterr()
        assert "Blocked by max-plan guard" in captured.out

    def test_successful_cycle(self, capsys):
        """Successful subprocess -> parse title -> notify telegram."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        cooldown_update = mock.Mock()
        log_fn = mock.Mock()

        fake_result = mock.Mock(
            stdout="# My Research Topic\nSome content with vault_write success and upsert_memory stored",
            stderr="",
            returncode=0,
        )
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", None),
            mock.patch("heartbeat.subprocess.run", return_value=fake_result),
            mock.patch("heartbeat._send_telegram") as mock_tg,
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="research",
                prompt_builder=mock.Mock(return_value="test prompt"),
                timeout=600,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=cooldown_update,
                log_fn=log_fn,
                emoji="\U0001f52c",
                label="Research Cycle",
                mpg_caller="research_cycle",
            )

        # Cooldown was updated with the title
        cooldown_update.assert_called_once_with("My Research Topic", success=True)
        # Telegram was notified
        mock_tg.assert_called_once()
        tg_text = mock_tg.call_args[0][0]
        assert "Research Cycle Complete" in tg_text
        assert "My Research Topic" in tg_text
        # Log was called (VAULT_WRITE, FUSION_MEMORY, COMPLETED)
        assert log_fn.call_count >= 3

    def test_timeout_handled(self, capsys):
        """subprocess.TimeoutExpired should log and update cooldown."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        cooldown_update = mock.Mock()
        log_fn = mock.Mock()

        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", None),
            mock.patch("heartbeat.subprocess.run", side_effect=subprocess_timeout()),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(return_value="prompt"),
                timeout=60,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=cooldown_update,
                log_fn=log_fn,
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        cooldown_update.assert_called_once_with("timeout", success=False)
        log_fn.assert_called_once()
        assert "TIMEOUT" in log_fn.call_args[0][0]
        captured = capsys.readouterr()
        assert "Timed out" in captured.out

    def test_empty_response_handled(self, capsys):
        """Empty stdout should log and update cooldown."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        cooldown_update = mock.Mock()
        log_fn = mock.Mock()

        fake_result = mock.Mock(stdout="", stderr="some error", returncode=1)
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", None),
            mock.patch("heartbeat.subprocess.run", return_value=fake_result),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(return_value="prompt"),
                timeout=60,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=cooldown_update,
                log_fn=log_fn,
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        cooldown_update.assert_called_once_with("unknown", success=False)
        log_fn.assert_called_once()
        assert "EMPTY_RESPONSE" in log_fn.call_args[0][0]

    def test_file_not_found_handled(self, capsys):
        """FileNotFoundError (missing claude binary) should log."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        log_fn = mock.Mock()

        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", None),
            mock.patch("heartbeat.subprocess.run", side_effect=FileNotFoundError("no claude")),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(return_value="prompt"),
                timeout=60,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=mock.Mock(),
                log_fn=log_fn,
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        log_fn.assert_called_once()
        assert "CLAUDE_NOT_FOUND" in log_fn.call_args[0][0]
        captured = capsys.readouterr()
        assert "Claude binary not found" in captured.out

    def test_generic_exception_handled(self, capsys):
        """Generic exceptions should log ERROR and update cooldown."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        cooldown_update = mock.Mock()
        log_fn = mock.Mock()

        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", None),
            mock.patch("heartbeat.subprocess.run", side_effect=RuntimeError("boom")),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="test",
                prompt_builder=mock.Mock(return_value="prompt"),
                timeout=60,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=cooldown_update,
                log_fn=log_fn,
                emoji="T",
                label="Test",
                mpg_caller="test",
            )
        log_fn.assert_called_once()
        assert "ERROR" in log_fn.call_args[0][0]
        cooldown_update.assert_called_once_with("error", success=False)

    def test_mpg_record_called_on_completion(self, capsys):
        """When _mpg_record is available, it should be called once on completion."""
        mock_dt = mock.MagicMock()
        mock_dt.hour = 12
        mock_mpg_record = mock.Mock()

        fake_result = mock.Mock(stdout="# Title\ncontent", stderr="", returncode=0)
        with (
            mock.patch("heartbeat.ACTIVE_HOURS_START", 0),
            mock.patch("heartbeat.ACTIVE_HOURS_END", 24),
            mock.patch("heartbeat.datetime") as mdt,
            mock.patch("heartbeat._mpg_allow", None),
            mock.patch("heartbeat._mpg_record", mock_mpg_record),
            mock.patch("heartbeat.subprocess.run", return_value=fake_result),
            mock.patch("heartbeat._send_telegram"),
        ):
            mdt.now.return_value = mock_dt
            mdt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            heartbeat._run_claude_cycle(
                cycle_name="research",
                prompt_builder=mock.Mock(return_value="prompt"),
                timeout=600,
                cooldown_check=mock.Mock(return_value=True),
                cooldown_update=mock.Mock(),
                log_fn=mock.Mock(),
                emoji="E",
                label="Label",
                mpg_caller="research_cycle",
            )
        mock_mpg_record.assert_called_once()
        call_kwargs = mock_mpg_record.call_args
        assert call_kwargs[1]["caller"] == "research_cycle"
        assert call_kwargs[1]["success"] is True
        assert call_kwargs[1]["duration_secs"] is not None


def subprocess_timeout():
    """Helper: returns a side effect that raises TimeoutExpired."""
    import subprocess

    return subprocess.TimeoutExpired(cmd="claude", timeout=60)


# ---------------------------------------------------------------------------
# Thin wrappers: _run_research_cycle and _run_planning_cycle
# ---------------------------------------------------------------------------


class TestThinWrappers:
    """_run_research_cycle and _run_planning_cycle delegate to _run_claude_cycle."""

    def test_research_delegates_with_paused(self):
        """_run_research_cycle passes paused=True."""
        with mock.patch("heartbeat._run_claude_cycle") as m:
            heartbeat._run_research_cycle()
        m.assert_called_once()
        kwargs = m.call_args[1]
        assert kwargs["cycle_name"] == "research"
        assert kwargs["paused"] is True
        assert kwargs["timeout"] == heartbeat.RESEARCH_TIMEOUT
        assert kwargs["mpg_caller"] == "research_cycle"
        assert kwargs["prompt_builder"] is heartbeat._build_research_prompt
        assert kwargs["cooldown_check"] is heartbeat._research_cooldown_ok
        assert kwargs["cooldown_update"] is heartbeat._update_research_cooldown
        assert kwargs["log_fn"] is heartbeat._log_research

    def test_planning_delegates_without_paused(self):
        """_run_planning_cycle passes default paused=False."""
        with mock.patch("heartbeat._run_claude_cycle") as m:
            heartbeat._run_planning_cycle()
        m.assert_called_once()
        kwargs = m.call_args[1]
        assert kwargs["cycle_name"] == "planning"
        assert kwargs.get("paused", False) is False
        assert kwargs["timeout"] == heartbeat.PLANNING_TIMEOUT
        assert kwargs["mpg_caller"] == "planning_cycle"
        assert kwargs["prompt_builder"] is heartbeat._build_planning_prompt
        assert kwargs["cooldown_check"] is heartbeat._planning_cooldown_ok
        assert kwargs["cooldown_update"] is heartbeat._update_planning_cooldown
        assert kwargs["log_fn"] is heartbeat._log_planning

    def test_research_label_and_emoji(self):
        with mock.patch("heartbeat._run_claude_cycle") as m:
            heartbeat._run_research_cycle()
        kwargs = m.call_args[1]
        assert kwargs["label"] == "Research Cycle"
        assert "🔬" in kwargs["emoji"] or kwargs["emoji"] == "\U0001f52c"

    def test_planning_label_and_emoji(self):
        with mock.patch("heartbeat._run_claude_cycle") as m:
            heartbeat._run_planning_cycle()
        kwargs = m.call_args[1]
        assert kwargs["label"] == "Planning Cycle"
        assert "📋" in kwargs["emoji"] or kwargs["emoji"] == "\U0001f4cb"


# ---------------------------------------------------------------------------
# Integration: _append_metrics wired into main() flow
# ---------------------------------------------------------------------------


class TestMetricsInMain:
    """Verify _append_metrics is called during main() execution."""

    def _stub_check(self, name):
        return {"name": name, "ok": True, "detail": "ok"}

    @mock.patch("heartbeat._run_planning_cycle")
    @mock.patch("heartbeat._run_research_cycle")
    @mock.patch("heartbeat._run_memory_maintenance")
    @mock.patch("heartbeat._run_heartbeat_agent")
    @mock.patch("heartbeat._send_telegram")
    @mock.patch("heartbeat.send_telegram_heartbeat")
    @mock.patch("heartbeat._append_metrics")
    def test_main_calls_append_metrics(self, m_metrics, m_tg_hb, m_tg, m_hb_agent, m_mem, m_res, m_plan, tmp_path):
        _make_tmp_base(tmp_path)
        check_names = [
            "check_service",
            "check_disk",
            "check_claude_binary",
            "check_task_queue",
            "check_last_output",
            "check_stale_workers",
            "check_state_files",
            "check_metrics",
            "check_google_workspace",
            "check_backup",
            "check_ruff",
            "check_memory_systems",
            "check_log_sizes",
            "check_state_bloat",
            "check_pip_audit",
            "check_llm_cache",
            "check_cost_router",
        ]
        patches = [mock.patch(f"heartbeat.{n}", return_value=self._stub_check(n)) for n in check_names]
        patches.append(mock.patch("heartbeat.SERVICES", ["test-svc"]))
        for p in patches:
            p.start()
        try:
            heartbeat.main()
        finally:
            for p in patches:
                p.stop()

        m_metrics.assert_called_once()
        snap = m_metrics.call_args[0][0]
        assert isinstance(snap, heartbeat.HeartbeatSnapshot)
        # Snapshot was created (status depends on dynamic imports, just check type)
        assert snap.status in ("healthy", "unhealthy")
        assert snap.duration_ms >= 0
        assert snap.total_checks > 0


# ---------------------------------------------------------------------------
# Phase 5.3: _parse_cycle_outcome()
# ---------------------------------------------------------------------------


class TestParseCycleOutcome:
    """_parse_cycle_outcome extracts structured CycleOutcome from freeform text."""

    def test_extracts_title_from_header(self):
        response = "# My Research Topic\nSome body text here."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.title == "My Research Topic"

    def test_extracts_title_from_h2_header(self):
        response = "## Sub-heading Title\nDetails follow."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.title == "Sub-heading Title"

    def test_title_unknown_when_no_header(self):
        response = "Just some plain text without any headers."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.title == "unknown"

    def test_title_truncated_at_200_chars(self):
        long_title = "A" * 300
        response = f"# {long_title}\nBody."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert len(outcome.title) <= 200

    def test_vault_write_success_keywords(self):
        from utils.schemas.heartbeat import WriteStatus

        for kw in ("success", "accepted", "written"):
            response = f"Called vault_write and got {kw}."
            outcome = heartbeat._parse_cycle_outcome(response)
            assert outcome.vault_write == WriteStatus.SUCCESS, f"Failed for keyword: {kw}"

    def test_vault_write_failed_keywords(self):
        from utils.schemas.heartbeat import WriteStatus

        for kw in ("error", "rejected", "failed", "invalid"):
            response = f"Called vault_write but got {kw}."
            outcome = heartbeat._parse_cycle_outcome(response)
            assert outcome.vault_write == WriteStatus.FAILED, f"Failed for keyword: {kw}"

    def test_vault_write_unknown_when_not_mentioned(self):
        from utils.schemas.heartbeat import WriteStatus

        response = "# Title\nDid some research, no vault involved."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.vault_write == WriteStatus.UNKNOWN

    def test_memory_write_success_keywords(self):
        from utils.schemas.heartbeat import WriteStatus

        for kw in ("success", "stored"):
            response = f"Called upsert_memory and it returned {kw}."
            outcome = heartbeat._parse_cycle_outcome(response)
            assert outcome.memory_write == WriteStatus.SUCCESS, f"Failed for keyword: {kw}"

    def test_memory_write_unknown_when_not_mentioned(self):
        from utils.schemas.heartbeat import WriteStatus

        response = "# Title\nResearch complete, no memory writes."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.memory_write == WriteStatus.UNKNOWN

    def test_empty_response_defaults(self):
        from utils.schemas.heartbeat import WriteStatus

        outcome = heartbeat._parse_cycle_outcome("")
        assert outcome.title == "unknown"
        assert outcome.vault_write == WriteStatus.UNKNOWN
        assert outcome.memory_write == WriteStatus.UNKNOWN

    def test_combined_vault_and_memory_success(self):
        from utils.schemas.heartbeat import WriteStatus

        response = "# Research Complete\nvault_write success confirmed.\nupsert_memory stored the findings."
        outcome = heartbeat._parse_cycle_outcome(response)
        assert outcome.title == "Research Complete"
        assert outcome.vault_write == WriteStatus.SUCCESS
        assert outcome.memory_write == WriteStatus.SUCCESS

    def test_returns_cycle_outcome_type(self):
        from utils.schemas.heartbeat import CycleOutcome

        outcome = heartbeat._parse_cycle_outcome("# Test\nBody")
        assert isinstance(outcome, CycleOutcome)


# ---------------------------------------------------------------------------
# Phase 5.3: Updated _extract_json_actions()
# ---------------------------------------------------------------------------


class TestExtractJsonActionsPhase53:
    """Updated _extract_json_actions uses structured_output._extract_json."""

    def test_valid_json_array(self):
        text = '[{"type": "notify", "message": "hello"}, {"type": "task", "title": "fix"}]'
        result = heartbeat._extract_json_actions(text)
        assert result is not None
        assert len(result) == 2
        assert result[0]["type"] == "notify"
        assert result[1]["type"] == "task"

    def test_json_in_code_block(self):
        text = 'Here is my response:\n```json\n[{"type": "notify", "message": "alert"}]\n```\nDone.'
        result = heartbeat._extract_json_actions(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["message"] == "alert"

    def test_no_json_returns_none(self):
        text = "This is just plain text with no JSON at all."
        result = heartbeat._extract_json_actions(text)
        assert result is None

    def test_invalid_json_returns_none(self):
        text = '[{"type": "notify", "message": invalid}]'
        result = heartbeat._extract_json_actions(text)
        assert result is None

    def test_json_object_not_array_returns_none(self):
        text = '{"type": "notify", "message": "hello"}'
        result = heartbeat._extract_json_actions(text)
        assert result is None

    def test_array_of_non_dicts_returns_none(self):
        text = '["a", "b", "c"]'
        result = heartbeat._extract_json_actions(text)
        assert result is None

    def test_nested_array_in_response(self):
        text = (
            'Some preamble text.\n[{"type": "notify", "message": "check [this] out", "tags": ["a", "b"]}]\nMore text.'
        )
        result = heartbeat._extract_json_actions(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["tags"] == ["a", "b"]

    def test_empty_array_returns_none(self):
        """Empty array has no dicts, so all() vacuously true — returns []."""
        text = "[]"
        result = heartbeat._extract_json_actions(text)
        # Empty list: all(isinstance(d, dict) for d in []) is True, returns []
        assert result == []
