"""Comprehensive tests for utils/security_monitor.py.

Tests cover:
- _empty_dashboard() template
- SecurityDashboard: __init__, _load, _save, update, get_dashboard,
  get_summary_text, check_anomalies
- Individual collectors: kill_switch, budget, circuit_breakers, task_audit,
  auth_failures, egress, secret_detections, audit_chain_valid
- Helper functions: _tail_file, _extract_log_timestamp
- format_dashboard_telegram formatter
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestEmptyDashboard(unittest.TestCase):
    """Tests for _empty_dashboard()."""

    def test_returns_dict(self):
        from utils.security_monitor import _empty_dashboard

        result = _empty_dashboard()
        self.assertIsInstance(result, dict)

    def test_zeroed_values(self):
        from utils.security_monitor import _empty_dashboard

        d = _empty_dashboard()
        self.assertEqual(d["last_updated"], "")
        self.assertEqual(d["input_sanitizer"]["blocked"], 0)
        self.assertEqual(d["input_sanitizer"]["flagged"], 0)
        self.assertEqual(d["input_sanitizer"]["passed"], 0)
        self.assertEqual(d["kill_switch"]["mode"], "run")
        self.assertTrue(d["kill_switch"]["heartbeat_alive"])
        self.assertEqual(d["circuit_breakers"], {})
        self.assertAlmostEqual(d["budget"]["daily_used_pct"], 0.0)
        self.assertAlmostEqual(d["budget"]["monthly_cost"], 0.0)
        self.assertEqual(d["egress_blocks"], 0)
        self.assertEqual(d["auth_failures_24h"], 0)
        self.assertEqual(d["secret_detections"], 0)
        self.assertEqual(d["task_validations"]["passed"], 0)
        self.assertEqual(d["task_validations"]["flagged"], 0)
        self.assertEqual(d["task_validations"]["blocked"], 0)
        self.assertTrue(d["audit_chain_valid"])
        self.assertAlmostEqual(d["uptime_hours"], 0.0)

    def test_returns_fresh_copy_each_call(self):
        from utils.security_monitor import _empty_dashboard

        d1 = _empty_dashboard()
        d2 = _empty_dashboard()
        d1["egress_blocks"] = 99
        self.assertEqual(d2["egress_blocks"], 0)


class TestSecurityDashboardInit(unittest.TestCase):
    """Tests for SecurityDashboard.__init__."""

    def test_init_no_state_file(self):
        """Init with no persisted state should produce empty dashboard."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                d = sd.get_dashboard()
                self.assertEqual(d["last_updated"], "")
                self.assertEqual(d["egress_blocks"], 0)

    def test_init_loads_persisted_state(self):
        """Init should load from STATE_FILE if it exists."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            saved = {
                "last_updated": "2026-01-01T00:00:00Z",
                "egress_blocks": 42,
                "budget": {"daily_used_pct": 55.0},
            }
            state_file.write_text(json.dumps(saved), encoding="utf-8")

            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                d = sd.get_dashboard()
                self.assertEqual(d["egress_blocks"], 42)
                self.assertEqual(d["last_updated"], "2026-01-01T00:00:00Z")


class TestSecurityDashboardLoad(unittest.TestCase):
    """Tests for SecurityDashboard._load()."""

    def test_load_missing_file(self):
        """Missing file should leave dashboard at defaults."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "nonexistent.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                self.assertEqual(sd._data["last_updated"], "")

    def test_load_corrupt_json(self):
        """Corrupt JSON should log warning and keep defaults."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            state_file.write_text("{not valid json!!!", encoding="utf-8")

            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                # Should still have the empty dashboard defaults
                self.assertEqual(sd._data["last_updated"], "")

    def test_load_empty_file(self):
        """Empty file should be treated as corrupt JSON and fall back to defaults."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            state_file.write_text("", encoding="utf-8")

            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                self.assertEqual(sd._data["last_updated"], "")

    def test_load_valid_json(self):
        """Valid JSON should be loaded as the dashboard data."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            data = {"last_updated": "test", "secret_detections": 7}
            state_file.write_text(json.dumps(data), encoding="utf-8")

            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                self.assertEqual(sd._data["last_updated"], "test")
                self.assertEqual(sd._data["secret_detections"], 7)


class TestSecurityDashboardSave(unittest.TestCase):
    """Tests for SecurityDashboard._save()."""

    def test_save_creates_state_dir(self):
        """_save should create STATE_DIR if it doesn't exist."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "new_state_dir"
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                sd._data["egress_blocks"] = 99
                sd._save()

                self.assertTrue(state_dir.exists())
                self.assertTrue(state_file.exists())
                loaded = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(loaded["egress_blocks"], 99)

    def test_save_overwrites_existing(self):
        """_save should overwrite any existing state file."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"
            state_file.write_text('{"old": true}', encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                sd._data = {"new": True}
                sd._save()

                loaded = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertTrue(loaded.get("new"))
                self.assertNotIn("old", loaded)

    def test_save_handles_permission_error(self):
        """_save should log warning on OSError, not raise."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", Path("/nonexistent_root_dir/state")),
            ):
                sd = SecurityDashboard()
                # Should not raise
                sd._save()


class TestSecurityDashboardUpdate(unittest.TestCase):
    """Tests for SecurityDashboard.update()."""

    def test_update_calls_all_collectors(self):
        """update() should call every collector and set last_updated."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()

                # Mock all collectors
                sd._collect_kill_switch = MagicMock(return_value={"mode": "run", "heartbeat_alive": True})
                sd._collect_budget = MagicMock(return_value={"daily_used_pct": 10.0, "monthly_cost": 5.0})
                sd._collect_circuit_breakers = MagicMock(return_value={})
                sd._collect_task_audit = MagicMock(return_value={"passed": 3, "flagged": 1, "blocked": 0})
                sd._collect_auth_failures = MagicMock(return_value=2)
                sd._collect_egress = MagicMock(return_value=0)
                sd._collect_secret_detections = MagicMock(return_value=0)
                sd._collect_audit_chain_valid = MagicMock(return_value=True)

                result = sd.update()

                sd._collect_kill_switch.assert_called_once()
                sd._collect_budget.assert_called_once()
                sd._collect_circuit_breakers.assert_called_once()
                sd._collect_task_audit.assert_called_once()
                sd._collect_auth_failures.assert_called_once()
                sd._collect_egress.assert_called_once()
                sd._collect_secret_detections.assert_called_once()
                sd._collect_audit_chain_valid.assert_called_once()

                # Result should have a timestamp
                self.assertNotEqual(result["last_updated"], "")
                self.assertEqual(result["auth_failures_24h"], 2)
                self.assertIsInstance(result["uptime_hours"], float)

    def test_update_persists_to_disk(self):
        """update() should save the result to STATE_FILE."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()

                # Mock all collectors to avoid real imports
                sd._collect_kill_switch = MagicMock(return_value={"mode": "run", "heartbeat_alive": True})
                sd._collect_budget = MagicMock(return_value={"daily_used_pct": 0.0, "monthly_cost": 0.0})
                sd._collect_circuit_breakers = MagicMock(return_value={})
                sd._collect_task_audit = MagicMock(return_value={"passed": 0, "flagged": 0, "blocked": 0})
                sd._collect_auth_failures = MagicMock(return_value=0)
                sd._collect_egress = MagicMock(return_value=0)
                sd._collect_secret_detections = MagicMock(return_value=0)
                sd._collect_audit_chain_valid = MagicMock(return_value=True)

                sd.update()

                self.assertTrue(state_file.exists())
                loaded = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertIn("last_updated", loaded)

    def test_update_returns_dict(self):
        """update() should return the updated dashboard dict."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                sd._collect_kill_switch = MagicMock(return_value={"mode": "run", "heartbeat_alive": True})
                sd._collect_budget = MagicMock(return_value={"daily_used_pct": 0.0, "monthly_cost": 0.0})
                sd._collect_circuit_breakers = MagicMock(return_value={})
                sd._collect_task_audit = MagicMock(return_value={"passed": 0, "flagged": 0, "blocked": 0})
                sd._collect_auth_failures = MagicMock(return_value=0)
                sd._collect_egress = MagicMock(return_value=0)
                sd._collect_secret_detections = MagicMock(return_value=0)
                sd._collect_audit_chain_valid = MagicMock(return_value=True)

                result = sd.update()
                self.assertIsInstance(result, dict)


class TestGetDashboard(unittest.TestCase):
    """Tests for SecurityDashboard.get_dashboard()."""

    def test_returns_copy(self):
        """get_dashboard should return a copy, not the internal dict."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                d1 = sd.get_dashboard()
                d1["egress_blocks"] = 999
                d2 = sd.get_dashboard()
                self.assertEqual(d2["egress_blocks"], 0)

    def test_reflects_internal_state(self):
        """get_dashboard should reflect internal _data."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                sd._data["auth_failures_24h"] = 42
                d = sd.get_dashboard()
                self.assertEqual(d["auth_failures_24h"], 42)


class TestGetSummaryText(unittest.TestCase):
    """Tests for SecurityDashboard.get_summary_text()."""

    def test_returns_string(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                text = sd.get_summary_text()
                self.assertIsInstance(text, str)
                self.assertIn("NovaCore Security Dashboard", text)


class TestCheckAnomalies(unittest.TestCase):
    """Tests for SecurityDashboard.check_anomalies()."""

    def _make_dashboard(self, **overrides):
        from utils.security_monitor import SecurityDashboard, _empty_dashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                sd._data = _empty_dashboard()
                for k, v in overrides.items():
                    sd._data[k] = v
                return sd.check_anomalies()

    def test_all_ok_no_anomalies(self):
        anomalies = self._make_dashboard()
        self.assertEqual(anomalies, [])

    def test_circuit_breaker_open(self):
        anomalies = self._make_dashboard(circuit_breakers={"claude_api": {"state": "OPEN", "trip_count": 3}})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("Circuit breaker 'claude_api' is OPEN", anomalies[0])

    def test_circuit_breaker_open_string_format(self):
        """Circuit breaker state can be a plain string."""
        anomalies = self._make_dashboard(circuit_breakers={"my_breaker": "OPEN"})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("my_breaker", anomalies[0])

    def test_circuit_breaker_closed_no_anomaly(self):
        anomalies = self._make_dashboard(circuit_breakers={"claude_api": {"state": "CLOSED", "trip_count": 0}})
        self.assertEqual(anomalies, [])

    def test_budget_above_90(self):
        anomalies = self._make_dashboard(budget={"daily_used_pct": 95.5, "monthly_cost": 0.0})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("95.5%", anomalies[0])
        self.assertIn("exceeds 90%", anomalies[0])

    def test_budget_exactly_90_no_anomaly(self):
        anomalies = self._make_dashboard(budget={"daily_used_pct": 90.0, "monthly_cost": 0.0})
        self.assertEqual(anomalies, [])

    def test_kill_switch_not_run(self):
        anomalies = self._make_dashboard(kill_switch={"mode": "pause", "heartbeat_alive": True})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("Kill switch mode is 'pause'", anomalies[0])

    def test_heartbeat_dead(self):
        anomalies = self._make_dashboard(kill_switch={"mode": "run", "heartbeat_alive": False})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("NOT alive", anomalies[0])

    def test_auth_failures_above_threshold(self):
        anomalies = self._make_dashboard(auth_failures_24h=10)
        self.assertEqual(len(anomalies), 1)
        self.assertIn("Auth failures in 24h: 10", anomalies[0])

    def test_auth_failures_exactly_5_no_anomaly(self):
        anomalies = self._make_dashboard(auth_failures_24h=5)
        self.assertEqual(anomalies, [])

    def test_tasks_blocked(self):
        anomalies = self._make_dashboard(task_validations={"passed": 5, "flagged": 2, "blocked": 1})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("Tasks blocked in last hour: 1", anomalies[0])

    def test_multiple_anomalies(self):
        """Multiple anomalies should all be reported."""
        anomalies = self._make_dashboard(
            kill_switch={"mode": "stop", "heartbeat_alive": False},
            budget={"daily_used_pct": 99.0, "monthly_cost": 0.0},
            circuit_breakers={"cb1": {"state": "OPEN", "trip_count": 1}},
            auth_failures_24h=20,
            task_validations={"passed": 0, "flagged": 0, "blocked": 3},
        )
        # kill_switch mode != run, heartbeat dead, budget > 90%, cb OPEN, auth > 5, tasks blocked
        self.assertEqual(len(anomalies), 6)


class TestTailFile(unittest.TestCase):
    """Tests for _tail_file()."""

    def test_small_file_reads_all(self):
        from utils.security_monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            f.flush()
            path = Path(f.name)

        try:
            result = _tail_file(path, max_bytes=4096)
            self.assertEqual(result, "line1\nline2\nline3\n")
        finally:
            path.unlink()

    def test_large_file_reads_tail(self):
        from utils.security_monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            # Write 1000 lines, each ~50 bytes
            for i in range(1000):
                f.write(f"line_{i:04d}: some log data here padding text\n")
            f.flush()
            path = Path(f.name)

        try:
            # Only read last 200 bytes
            result = _tail_file(path, max_bytes=200)
            lines = result.strip().splitlines()
            # Should have fewer lines than original
            self.assertLess(len(lines), 1000)
            # Last line should be line_0999
            self.assertIn("line_0999", lines[-1])
            # First line in result should NOT be line_0000 (we skipped the beginning)
            self.assertNotIn("line_0000", lines[0])
        finally:
            path.unlink()

    def test_file_exactly_max_bytes(self):
        from utils.security_monitor import _tail_file

        content = "abcdef\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        try:
            result = _tail_file(path, max_bytes=len(content.encode("utf-8")))
            self.assertEqual(result, content)
        finally:
            path.unlink()

    def test_single_line_file(self):
        from utils.security_monitor import _tail_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("single line no newline")
            f.flush()
            path = Path(f.name)

        try:
            result = _tail_file(path, max_bytes=4096)
            self.assertEqual(result, "single line no newline")
        finally:
            path.unlink()

    def test_default_max_bytes(self):
        """_tail_file should have a default max_bytes of 256_000."""
        import inspect

        from utils.security_monitor import _tail_file

        sig = inspect.signature(_tail_file)
        default = sig.parameters["max_bytes"].default
        self.assertEqual(default, 256_000)


class TestExtractLogTimestamp(unittest.TestCase):
    """Tests for _extract_log_timestamp()."""

    def test_iso_format(self):
        from utils.security_monitor import _extract_log_timestamp

        line = "2026-03-12T10:30:00 AUTH_DENIED user=foo"
        ts = _extract_log_timestamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 3)
        self.assertEqual(ts.day, 12)
        self.assertEqual(ts.hour, 10)
        self.assertEqual(ts.minute, 30)
        self.assertEqual(ts.tzinfo, timezone.utc)

    def test_standard_log_format(self):
        from utils.security_monitor import _extract_log_timestamp

        line = "2026-03-12 10:30:00 WARNING AUTH_DENIED"
        ts = _extract_log_timestamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.hour, 10)

    def test_unparseable_line(self):
        from utils.security_monitor import _extract_log_timestamp

        line = "No timestamp here at all"
        ts = _extract_log_timestamp(line)
        self.assertIsNone(ts)

    def test_empty_string(self):
        from utils.security_monitor import _extract_log_timestamp

        ts = _extract_log_timestamp("")
        self.assertIsNone(ts)

    def test_partial_date_no_time(self):
        from utils.security_monitor import _extract_log_timestamp

        line = "2026-03-12 some event"
        ts = _extract_log_timestamp(line)
        # The regex requires time component, so this should be None
        self.assertIsNone(ts)

    def test_iso_with_timezone_suffix(self):
        from utils.security_monitor import _extract_log_timestamp

        line = "2026-03-12T14:00:00+00:00 some event"
        ts = _extract_log_timestamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(ts.hour, 14)

    def test_naive_timestamp_gets_utc(self):
        """A timestamp without tz info should be assumed UTC."""
        from utils.security_monitor import _extract_log_timestamp

        line = "2026-01-01T00:00:00 event"
        ts = _extract_log_timestamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(ts.tzinfo, timezone.utc)


class TestFormatDashboardTelegram(unittest.TestCase):
    """Tests for format_dashboard_telegram()."""

    def test_full_data_ok(self):
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "2026-03-12T10:00:00Z",
            "uptime_hours": 12.5,
            "kill_switch": {"mode": "run", "heartbeat_alive": True},
            "budget": {"daily_used_pct": 30.0, "monthly_cost": 10.0, "monthly_cap": 50.0},
            "circuit_breakers": {
                "claude_api": {"state": "CLOSED", "trip_count": 0},
            },
            "task_validations": {"passed": 10, "flagged": 1, "blocked": 0},
            "auth_failures_24h": 0,
            "egress_blocks": 0,
            "secret_detections": 0,
            "audit_chain_valid": True,
        }

        text = format_dashboard_telegram(data)

        self.assertIn("=== NovaCore Security Dashboard ===", text)
        self.assertIn("=== End Dashboard ===", text)
        self.assertIn("2026-03-12T10:00:00Z", text)
        self.assertIn("12.5h", text)

        # All OK statuses
        self.assertIn("[OK] mode=run", text)
        self.assertIn("[OK] alive=True", text)
        self.assertIn("[OK] daily=30.0%", text)
        self.assertIn("claude_api: [OK] CLOSED", text)
        self.assertNotIn("[CRIT]", text)
        self.assertNotIn("[WARN]", text)

    def test_critical_indicators(self):
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "2026-03-12T10:00:00Z",
            "uptime_hours": 0.5,
            "kill_switch": {"mode": "stop", "heartbeat_alive": False},
            "budget": {"daily_used_pct": 95.0, "monthly_cost": 45.0, "monthly_cap": 50.0},
            "circuit_breakers": {
                "claude_api": {"state": "OPEN", "trip_count": 5},
            },
            "task_validations": {"passed": 0, "flagged": 0, "blocked": 0},
            "auth_failures_24h": 0,
            "egress_blocks": 0,
            "secret_detections": 3,
            "audit_chain_valid": False,
        }

        text = format_dashboard_telegram(data)

        self.assertIn("[CRIT] mode=stop", text)
        self.assertIn("[CRIT] alive=False", text)
        self.assertIn("[CRIT] daily=95.0%", text)
        self.assertIn("claude_api: [CRIT] OPEN (trips=5)", text)
        self.assertIn("[CRIT] detections=3", text)
        self.assertIn("[CRIT] valid=False", text)

    def test_warn_indicators(self):
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "2026-03-12T10:00:00Z",
            "uptime_hours": 1.0,
            "kill_switch": {"mode": "run", "heartbeat_alive": True},
            "budget": {"daily_used_pct": 80.0, "monthly_cost": 10.0, "monthly_cap": 50.0},
            "circuit_breakers": {
                "tool_exec": {"state": "HALF_OPEN", "trip_count": 2},
            },
            "task_validations": {"passed": 5, "flagged": 1, "blocked": 2},
            "auth_failures_24h": 10,
            "egress_blocks": 3,
            "secret_detections": 0,
            "audit_chain_valid": True,
        }

        text = format_dashboard_telegram(data)

        self.assertIn("[WARN] daily=80.0%", text)
        self.assertIn("tool_exec: [WARN] HALF_OPEN (trips=2)", text)
        self.assertIn("[WARN]", text)
        # Tasks blocked -> WARN
        self.assertIn("[WARN] passed=5 flagged=1 blocked=2", text)
        # Auth > 5 -> WARN
        self.assertIn("[WARN] failures=10", text)
        # Egress > 0 -> WARN
        self.assertIn("[WARN] blocked=3", text)

    def test_no_circuit_breakers(self):
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "",
            "uptime_hours": 0,
            "kill_switch": {"mode": "run", "heartbeat_alive": True},
            "budget": {"daily_used_pct": 0.0, "monthly_cost": 0.0, "monthly_cap": 0.0},
            "circuit_breakers": {},
            "task_validations": {"passed": 0, "flagged": 0, "blocked": 0},
            "auth_failures_24h": 0,
            "egress_blocks": 0,
            "secret_detections": 0,
            "audit_chain_valid": True,
        }

        text = format_dashboard_telegram(data)
        self.assertIn("Circuit Breakers: [OK] none tripped", text)

    def test_circuit_breaker_with_underscore_prefix_skipped(self):
        """Keys starting with _ (like _error) should be skipped in output."""
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "",
            "uptime_hours": 0,
            "kill_switch": {"mode": "run", "heartbeat_alive": True},
            "budget": {"daily_used_pct": 0.0, "monthly_cost": 0.0, "monthly_cap": 0.0},
            "circuit_breakers": {"_error": "some import error"},
            "task_validations": {"passed": 0, "flagged": 0, "blocked": 0},
            "auth_failures_24h": 0,
            "egress_blocks": 0,
            "secret_detections": 0,
            "audit_chain_valid": True,
        }

        text = format_dashboard_telegram(data)
        # _error should not appear as a circuit breaker entry
        self.assertNotIn("_error", text)

    def test_circuit_breaker_plain_string_state(self):
        """Circuit breaker info can be a plain string instead of dict."""
        from utils.security_monitor import format_dashboard_telegram

        data = {
            "last_updated": "",
            "uptime_hours": 0,
            "kill_switch": {"mode": "run", "heartbeat_alive": True},
            "budget": {"daily_used_pct": 0.0, "monthly_cost": 0.0, "monthly_cap": 0.0},
            "circuit_breakers": {"my_cb": "CLOSED"},
            "task_validations": {"passed": 0, "flagged": 0, "blocked": 0},
            "auth_failures_24h": 0,
            "egress_blocks": 0,
            "secret_detections": 0,
            "audit_chain_valid": True,
        }

        text = format_dashboard_telegram(data)
        self.assertIn("my_cb: [OK] CLOSED (trips=0)", text)

    def test_missing_keys_use_defaults(self):
        """format_dashboard_telegram should handle missing keys gracefully."""
        from utils.security_monitor import format_dashboard_telegram

        data = {}
        text = format_dashboard_telegram(data)
        self.assertIn("NovaCore Security Dashboard", text)
        self.assertIn("Updated: unknown", text)


class TestCollectKillSwitch(unittest.TestCase):
    """Tests for SecurityDashboard._collect_kill_switch()."""

    def test_success(self):
        from utils.security_monitor import SecurityDashboard

        mock_ks_module = MagicMock()
        mock_ks_module.check_kill_switch.return_value = "run"
        mock_ks_module.check_dead_man_switch.return_value = True

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch.dict("sys.modules", {"nova_kill_switch": mock_ks_module}),
            ):
                sd = SecurityDashboard()
                result = sd._collect_kill_switch()
                self.assertEqual(result["mode"], "run")
                self.assertTrue(result["heartbeat_alive"])

    def test_kill_switch_paused(self):
        from utils.security_monitor import SecurityDashboard

        mock_ks_module = MagicMock()
        mock_ks_module.check_kill_switch.return_value = "pause"
        mock_ks_module.check_dead_man_switch.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch.dict("sys.modules", {"nova_kill_switch": mock_ks_module}),
            ):
                sd = SecurityDashboard()
                result = sd._collect_kill_switch()
                self.assertEqual(result["mode"], "pause")
                self.assertFalse(result["heartbeat_alive"])

    def test_import_error_returns_defaults_with_error(self):
        """If nova_kill_switch can't be imported, return defaults with error."""
        import sys

        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()

                # Remove nova_kill_switch from sys.modules if cached, then
                # make the import fail by inserting a broken module
                saved = sys.modules.pop("nova_kill_switch", None)
                try:
                    # Insert a sentinel that raises on attribute access
                    broken = MagicMock()
                    broken.check_kill_switch.side_effect = RuntimeError("broken")
                    broken.check_dead_man_switch.side_effect = RuntimeError("broken")
                    sys.modules["nova_kill_switch"] = broken

                    result = sd._collect_kill_switch()
                    # Should fall through to except branch
                    self.assertEqual(result["mode"], "run")
                    self.assertTrue(result["heartbeat_alive"])
                    self.assertIn("error", result)
                finally:
                    sys.modules.pop("nova_kill_switch", None)
                    if saved is not None:
                        sys.modules["nova_kill_switch"] = saved


class TestCollectBudget(unittest.TestCase):
    """Tests for SecurityDashboard._collect_budget()."""

    def test_success(self):
        from utils.security_monitor import SecurityDashboard

        mock_budget = MagicMock()
        mock_budget.get_usage_summary.return_value = {"daily": {"pct_input": 45.0}}
        mock_budget.get_cost_summary.return_value = {
            "monthly_cost_usd": 12.50,
            "monthly_cap_usd": 50.0,
            "monthly_pct": 25.0,
        }

        mock_module = MagicMock()
        mock_module.budget = mock_budget

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch.dict("sys.modules", {"agents.budget_enforcer": mock_module, "agents": MagicMock()}),
            ):
                sd = SecurityDashboard()
                result = sd._collect_budget()
                self.assertAlmostEqual(result["daily_used_pct"], 45.0)
                self.assertAlmostEqual(result["monthly_cost"], 12.50)
                self.assertAlmostEqual(result["monthly_cap"], 50.0)
                self.assertAlmostEqual(result["monthly_pct"], 25.0)

    def test_import_error_returns_defaults(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()

                original_import = __import__

                def failing_import(name, *args, **kwargs):
                    if "budget_enforcer" in name:
                        raise ImportError("no module")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=failing_import):
                    result = sd._collect_budget()
                    self.assertAlmostEqual(result["daily_used_pct"], 0.0)
                    self.assertAlmostEqual(result["monthly_cost"], 0.0)
                    self.assertIn("error", result)


class TestCollectTaskAudit(unittest.TestCase):
    """Tests for SecurityDashboard._collect_task_audit()."""

    def test_no_audit_file(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_task_audit()
                self.assertEqual(result, {"passed": 0, "flagged": 0, "blocked": 0})

    def test_counts_recent_entries(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"
            audit_file = state_dir / "task_audit.jsonl"

            now = datetime.now(timezone.utc)
            recent_ts = (now - timedelta(minutes=10)).isoformat()
            old_ts = (now - timedelta(hours=2)).isoformat()

            entries = [
                json.dumps({"timestamp": recent_ts, "blocked": False, "risk_score": 10}),
                json.dumps({"timestamp": recent_ts, "blocked": False, "risk_score": 60}),
                json.dumps({"timestamp": recent_ts, "blocked": True, "risk_score": 90}),
                json.dumps({"timestamp": old_ts, "blocked": True, "risk_score": 90}),  # too old
            ]
            audit_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_task_audit()
                self.assertEqual(result["passed"], 1)
                self.assertEqual(result["flagged"], 1)
                self.assertEqual(result["blocked"], 1)

    def test_handles_corrupt_lines(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"
            audit_file = state_dir / "task_audit.jsonl"

            now = datetime.now(timezone.utc)
            recent_ts = (now - timedelta(minutes=5)).isoformat()

            lines = [
                "not valid json",
                "",
                json.dumps({"timestamp": recent_ts, "blocked": False, "risk_score": 10}),
            ]
            audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_task_audit()
                self.assertEqual(result["passed"], 1)

    def test_empty_audit_file(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"
            audit_file = state_dir / "task_audit.jsonl"
            audit_file.write_text("", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_task_audit()
                self.assertEqual(result, {"passed": 0, "flagged": 0, "blocked": 0})


class TestCollectAuthFailures(unittest.TestCase):
    """Tests for SecurityDashboard._collect_auth_failures()."""

    def test_no_log_files(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "LOGS"
            logs_dir.mkdir()
            state_file = Path(tmpdir) / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                self.assertEqual(result, 0)

    def test_counts_auth_denied_in_logs(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"

            now = datetime.now(timezone.utc)
            recent_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            old_ts = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")

            watcher_log = logs_dir / "watcher.log"
            watcher_log.write_text(
                f"{recent_ts} AUTH_DENIED user=foo\n"
                f"{recent_ts} AUTH_DENIED user=bar\n"
                f"{old_ts} AUTH_DENIED user=baz\n"  # too old
                f"{recent_ts} Normal log line\n",
                encoding="utf-8",
            )

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                # 2 recent AUTH_DENIED, 1 old (should be excluded)
                self.assertEqual(result, 2)

    def test_counts_auth_denied_no_timestamp(self):
        """Lines with AUTH_DENIED but no parseable timestamp should be counted."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"

            watcher_log = logs_dir / "watcher.log"
            watcher_log.write_text(
                "AUTH_DENIED user=foo no timestamp\n",
                encoding="utf-8",
            )

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                self.assertEqual(result, 1)

    def test_audit_dir_structured_logs(self):
        """Should count security.auth_denied events in structured audit logs."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()

            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            recent_ts = (now - timedelta(hours=1)).isoformat()

            audit_file = audit_dir / f"audit_{today}.jsonl"
            entries = [
                json.dumps({"event": "security.auth_denied", "timestamp": recent_ts}),
                json.dumps({"event": "security.auth_denied", "timestamp": recent_ts}),
                json.dumps({"event": "other_event", "timestamp": recent_ts}),
            ]
            audit_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                self.assertEqual(result, 2)

    def test_audit_dir_missing_is_ok(self):
        """No audit dir should not cause errors."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            # No audit dir created

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                self.assertEqual(result, 0)

    def test_audit_entry_no_timestamp_counted(self):
        """Auth denied entries with unparseable timestamps should be counted."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            entries = [
                json.dumps({"event": "security.auth_denied", "timestamp": "not-a-date"}),
            ]
            audit_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_auth_failures()
                self.assertEqual(result, 1)


class TestCollectSecretDetections(unittest.TestCase):
    """Tests for SecurityDashboard._collect_secret_detections()."""

    def test_no_audit_dir(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "LOGS"
            logs_dir.mkdir()
            state_file = Path(tmpdir) / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_secret_detections()
                self.assertEqual(result, 0)

    def test_counts_secret_detected_events(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            entries = [
                json.dumps({"event": "security.secret_detected", "detail": "api_key"}),
                json.dumps({"event": "security.secret_detected", "detail": "password"}),
                json.dumps({"event": "other_event"}),
                "",
                "not json",
            ]
            audit_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_secret_detections()
                self.assertEqual(result, 2)

    def test_no_audit_file_for_today(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()
            # No audit file for today

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()
                result = sd._collect_secret_detections()
                self.assertEqual(result, 0)


class TestCollectAuditChainValid(unittest.TestCase):
    """Tests for SecurityDashboard._collect_audit_chain_valid()."""

    def test_no_audit_file_returns_true(self):
        """No audit file for today should return True (not an error)."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()
            # No audit file for today

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()

                with patch("utils.security_monitor.LOGS_DIR", logs_dir):
                    result = sd._collect_audit_chain_valid()
                    self.assertTrue(result)

    def test_valid_chain(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            audit_file.write_text('{"event": "test"}\n', encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
                patch("utils.audit_log.verify_chain", return_value=(True, [])),
            ):
                sd = SecurityDashboard()
                result = sd._collect_audit_chain_valid()
                self.assertTrue(result)

    def test_invalid_chain(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"
            audit_dir = logs_dir / "audit"
            audit_dir.mkdir()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            audit_file.write_text('{"event": "test"}\n', encoding="utf-8")

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
                patch("utils.audit_log.verify_chain", return_value=(False, ["hash mismatch"])),
            ):
                sd = SecurityDashboard()
                result = sd._collect_audit_chain_valid()
                self.assertFalse(result)

    def test_import_error_returns_true(self):
        """If verify_chain can't be imported, assume valid to avoid false alarms."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            state_file = Path(tmpdir) / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.LOGS_DIR", logs_dir),
            ):
                sd = SecurityDashboard()

                original_import = __import__

                def failing_import(name, *args, **kwargs):
                    if "audit_log" in name:
                        raise ImportError("no module")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=failing_import):
                    result = sd._collect_audit_chain_valid()
                    self.assertTrue(result)


class TestCollectCircuitBreakers(unittest.TestCase):
    """Tests for SecurityDashboard._collect_circuit_breakers()."""

    def test_success(self):
        from utils.security_monitor import SecurityDashboard

        mock_api_breaker = MagicMock()
        mock_api_breaker.name = "claude_api"
        mock_api_breaker.state = "CLOSED"
        mock_api_breaker.trip_count = 0

        mock_tool_breaker = MagicMock()
        mock_tool_breaker.name = "tool_execution"
        mock_tool_breaker.state = "OPEN"
        mock_tool_breaker.trip_count = 3

        mock_cb_module = MagicMock()
        mock_cb_module.claude_api_breaker = mock_api_breaker
        mock_cb_module.tool_execution_breaker = mock_tool_breaker
        mock_cb_module._mcp_breakers = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch.dict(
                    "sys.modules",
                    {
                        "agents": MagicMock(),
                        "agents.circuit_breakers": mock_cb_module,
                    },
                ),
            ):
                sd = SecurityDashboard()
                result = sd._collect_circuit_breakers()
                self.assertEqual(result["claude_api"]["state"], "CLOSED")
                self.assertEqual(result["claude_api"]["trip_count"], 0)
                self.assertEqual(result["tool_execution"]["state"], "OPEN")
                self.assertEqual(result["tool_execution"]["trip_count"], 3)

    def test_with_mcp_breakers(self):
        from utils.security_monitor import SecurityDashboard

        mock_api_breaker = MagicMock()
        mock_api_breaker.name = "claude_api"
        mock_api_breaker.state = "CLOSED"
        mock_api_breaker.trip_count = 0

        mock_tool_breaker = MagicMock()
        mock_tool_breaker.name = "tool_execution"
        mock_tool_breaker.state = "CLOSED"
        mock_tool_breaker.trip_count = 0

        mock_mcp = MagicMock()
        mock_mcp.state = "HALF_OPEN"
        mock_mcp.trip_count = 2

        mock_cb_module = MagicMock()
        mock_cb_module.claude_api_breaker = mock_api_breaker
        mock_cb_module.tool_execution_breaker = mock_tool_breaker
        mock_cb_module._mcp_breakers = {"nova-memory": mock_mcp}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch.dict(
                    "sys.modules",
                    {
                        "agents": MagicMock(),
                        "agents.circuit_breakers": mock_cb_module,
                    },
                ),
            ):
                sd = SecurityDashboard()
                result = sd._collect_circuit_breakers()
                self.assertEqual(result["nova-memory"]["state"], "HALF_OPEN")
                self.assertEqual(result["nova-memory"]["trip_count"], 2)

    def test_import_error(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()

                original_import = __import__

                def failing_import(name, *args, **kwargs):
                    if "circuit_breakers" in name:
                        raise ImportError("no module")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=failing_import):
                    result = sd._collect_circuit_breakers()
                    self.assertIn("_error", result)


class TestCollectEgress(unittest.TestCase):
    """Tests for SecurityDashboard._collect_egress()."""

    def test_no_syslog_returns_zero(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()
                # Mock all syslog paths to not exist
                with patch("pathlib.Path.exists", return_value=False):
                    result = sd._collect_egress()
                    self.assertEqual(result, 0)

    def test_counts_egress_blocked(self):
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "security_dashboard.json"
            syslog = Path(tmpdir) / "syslog"
            syslog.write_text(
                "Mar 12 10:00:00 host kernel: EGRESS-BLOCKED src=1.2.3.4\n"
                "Mar 12 10:01:00 host kernel: EGRESS-BLOCKED src=5.6.7.8\n"
                "Mar 12 10:02:00 host kernel: normal log line\n",
                encoding="utf-8",
            )

            with patch.object(SecurityDashboard, "STATE_FILE", state_file):
                sd = SecurityDashboard()

                # Patch the syslog_paths list inside the method
                # We need to patch Path("/var/log/syslog") to point to our temp file
                original_exists = Path.exists

                def mock_exists(self_path):
                    if str(self_path) == "/var/log/syslog":
                        return True
                    return original_exists(self_path)

                with (
                    patch.object(Path, "exists", mock_exists),
                    patch("utils.security_monitor._tail_file", return_value=syslog.read_text()),
                ):
                    result = sd._collect_egress()
                    self.assertEqual(result, 2)


class TestIntegrationScenarios(unittest.TestCase):
    """Integration-style tests combining multiple components."""

    def test_full_lifecycle(self):
        """Init -> update -> get_dashboard -> check_anomalies -> get_summary_text."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                sd = SecurityDashboard()

                # Mock collectors
                sd._collect_kill_switch = MagicMock(return_value={"mode": "run", "heartbeat_alive": True})
                sd._collect_budget = MagicMock(
                    return_value={"daily_used_pct": 50.0, "monthly_cost": 20.0, "monthly_cap": 50.0}
                )
                sd._collect_circuit_breakers = MagicMock(return_value={"api": {"state": "CLOSED", "trip_count": 0}})
                sd._collect_task_audit = MagicMock(return_value={"passed": 5, "flagged": 0, "blocked": 0})
                sd._collect_auth_failures = MagicMock(return_value=1)
                sd._collect_egress = MagicMock(return_value=0)
                sd._collect_secret_detections = MagicMock(return_value=0)
                sd._collect_audit_chain_valid = MagicMock(return_value=True)

                # Update
                data = sd.update()
                self.assertNotEqual(data["last_updated"], "")

                # Get dashboard
                d = sd.get_dashboard()
                self.assertEqual(d["budget"]["daily_used_pct"], 50.0)

                # Check anomalies - should be empty (all OK)
                anomalies = sd.check_anomalies()
                self.assertEqual(anomalies, [])

                # Get summary text
                text = sd.get_summary_text()
                self.assertIn("[OK]", text)
                self.assertNotIn("[CRIT]", text)

    def test_persist_and_reload(self):
        """Saved state should be loadable by a new instance."""
        from utils.security_monitor import SecurityDashboard

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            state_file = state_dir / "security_dashboard.json"

            with (
                patch.object(SecurityDashboard, "STATE_FILE", state_file),
                patch("utils.security_monitor.STATE_DIR", state_dir),
            ):
                # First instance: set some data and save
                sd1 = SecurityDashboard()
                sd1._data["auth_failures_24h"] = 77
                sd1._data["last_updated"] = "2026-03-12T00:00:00Z"
                sd1._save()

                # Second instance: should load persisted data
                sd2 = SecurityDashboard()
                d = sd2.get_dashboard()
                self.assertEqual(d["auth_failures_24h"], 77)
                self.assertEqual(d["last_updated"], "2026-03-12T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
