"""Tests for heartbeat.py health checks."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import heartbeat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_base(tmp_path: Path) -> None:
    """Set heartbeat module paths to a temp directory."""
    heartbeat.BASE = tmp_path
    heartbeat.HEARTBEAT_FILE = tmp_path / "HEARTBEAT.md"
    heartbeat.STATE_DIR = tmp_path / "STATE"
    heartbeat.TASKS_DIR = tmp_path / "TASKS"
    heartbeat.OUTPUT_DIR = tmp_path / "OUTPUT"
    heartbeat.LOGS_DIR = tmp_path / "LOGS"
    for d in (heartbeat.STATE_DIR, heartbeat.TASKS_DIR,
              heartbeat.OUTPUT_DIR, heartbeat.LOGS_DIR,
              heartbeat.STATE_DIR / "running"):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# check_service
# ---------------------------------------------------------------------------

class TestCheckService:
    def test_active_service(self):
        with mock.patch("heartbeat.subprocess.run") as m:
            m.side_effect = [
                mock.Mock(stdout="active\n"),
                mock.Mock(stdout="MainPID=1234\nActiveEnterTimestamp=Mon 2026-03-03 10:00:00 UTC\n"),
            ]
            result = heartbeat.check_service("novacore-watcher")
        assert result["ok"] is True
        assert "1234" in result["detail"]

    def test_inactive_service(self):
        with mock.patch("heartbeat.subprocess.run") as m:
            m.return_value = mock.Mock(stdout="inactive\n")
            result = heartbeat.check_service("novacore-watcher")
        assert result["ok"] is False
        assert "NOT ACTIVE" in result["detail"]

    def test_check_failure(self):
        with mock.patch("heartbeat.subprocess.run", side_effect=OSError("nope")):
            result = heartbeat.check_service("novacore-watcher")
        assert result["ok"] is False
        assert "check failed" in result["detail"]


# ---------------------------------------------------------------------------
# check_disk
# ---------------------------------------------------------------------------

class TestCheckDisk:
    def test_healthy_disk(self):
        result = heartbeat.check_disk()
        assert result["ok"] is True
        assert "%" in result["detail"]
        assert "GB free" in result["detail"]

    def test_full_disk(self):
        fake_statvfs = mock.Mock()
        fake_statvfs.f_blocks = 1000
        fake_statvfs.f_frsize = 4096
        fake_statvfs.f_bavail = 10  # ~1% free → 99% used
        with mock.patch("os.statvfs", return_value=fake_statvfs):
            result = heartbeat.check_disk()
        assert result["ok"] is False
        assert "99" in result["detail"]


# ---------------------------------------------------------------------------
# check_claude_binary
# ---------------------------------------------------------------------------

class TestCheckClaudeBinary:
    def test_exists(self):
        result = heartbeat.check_claude_binary()
        # On the actual VPS this should pass
        assert result["name"] == "claude_binary"
        assert isinstance(result["ok"], bool)

    def test_missing(self):
        with mock.patch.object(Path, "exists", return_value=False):
            result = heartbeat.check_claude_binary()
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# check_task_queue
# ---------------------------------------------------------------------------

class TestCheckTaskQueue:
    def test_empty_queue(self, tmp_path):
        _make_tmp_base(tmp_path)
        result = heartbeat.check_task_queue()
        assert result["ok"] is True
        assert "0 pending" in result["detail"]

    def test_pending_tasks(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "TASKS" / "0001_foo.md").write_text("x")
        (tmp_path / "TASKS" / "0002_bar.md").write_text("x")
        result = heartbeat.check_task_queue()
        assert result["ok"] is True
        assert "2 pending" in result["detail"]

    def test_orphaned_inprogress(self, tmp_path):
        _make_tmp_base(tmp_path)
        ip = tmp_path / "TASKS" / "0001_old.md.inprogress"
        ip.write_text("x")
        # Backdate mtime to make it orphaned
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
        os.utime(ip, (old_ts, old_ts))
        result = heartbeat.check_task_queue()
        assert result["ok"] is False
        assert "ORPHANED" in result["detail"]

    def test_too_many_pending(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(15):
            (tmp_path / "TASKS" / f"{i:04d}_task.md").write_text("x")
        result = heartbeat.check_task_queue()
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# check_last_output
# ---------------------------------------------------------------------------

class TestCheckLastOutput:
    def test_no_outputs(self, tmp_path):
        _make_tmp_base(tmp_path)
        result = heartbeat.check_last_output()
        assert result["ok"] is True

    def test_recent_output(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "OUTPUT" / "test.md").write_text("x")
        result = heartbeat.check_last_output()
        assert result["ok"] is True
        assert "test.md" in result["detail"]


# ---------------------------------------------------------------------------
# check_stale_workers
# ---------------------------------------------------------------------------

class TestCheckStaleWorkers:
    def test_no_running(self, tmp_path):
        _make_tmp_base(tmp_path)
        result = heartbeat.check_stale_workers()
        assert result["ok"] is True

    def test_alive_worker(self, tmp_path):
        _make_tmp_base(tmp_path)
        pid_file = tmp_path / "STATE" / "running" / "test.pid"
        pid_file.write_text(str(os.getpid()))  # our own PID — alive
        result = heartbeat.check_stale_workers()
        assert result["ok"] is True

    def test_dead_worker(self, tmp_path):
        _make_tmp_base(tmp_path)
        pid_file = tmp_path / "STATE" / "running" / "test.pid"
        pid_file.write_text("999999")  # very unlikely to be alive
        result = heartbeat.check_stale_workers()
        assert result["ok"] is False
        assert "stale" in result["detail"]


# ---------------------------------------------------------------------------
# check_metrics
# ---------------------------------------------------------------------------

class TestCheckMetrics:
    def test_no_metrics_file(self, tmp_path):
        _make_tmp_base(tmp_path)
        result = heartbeat.check_metrics()
        assert result["ok"] is True

    def test_healthy_metrics(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text(json.dumps({
            "contract_success": {"_total": 90},
            "contract_failure": {"_total": 10},
        }))
        result = heartbeat.check_metrics()
        assert result["ok"] is True
        assert "10.0%" in result["detail"]

    def test_unhealthy_metrics(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text(json.dumps({
            "contract_success": {"_total": 20},
            "contract_failure": {"_total": 80},
        }))
        result = heartbeat.check_metrics()
        assert result["ok"] is False
        assert "80.0%" in result["detail"]

    def test_plain_int_metrics(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text(json.dumps({
            "contract_success": 9,
            "contract_failure": 1,
        }))
        result = heartbeat.check_metrics()
        assert result["ok"] is True

    def test_corrupt_json(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text("{bad json")
        result = heartbeat.check_metrics()
        assert result["ok"] is False
        assert "parse error" in result["detail"]


# ---------------------------------------------------------------------------
# write_heartbeat
# ---------------------------------------------------------------------------

class TestWriteHeartbeat:
    def test_writes_file(self, tmp_path):
        _make_tmp_base(tmp_path)
        checks = [
            {"name": "test1", "ok": True, "detail": "good"},
            {"name": "test2", "ok": False, "detail": "bad"},
        ]
        heartbeat.write_heartbeat(checks)
        content = heartbeat.HEARTBEAT_FILE.read_text()
        assert "- [x] test1: good" in content
        assert "- [ ] test2: bad" in content
        assert "UNHEALTHY" in content

    def test_all_healthy(self, tmp_path):
        _make_tmp_base(tmp_path)
        checks = [{"name": "test", "ok": True, "detail": "ok"}]
        heartbeat.write_heartbeat(checks)
        content = heartbeat.HEARTBEAT_FILE.read_text()
        assert "HEALTHY" in content
        assert "UNHEALTHY" not in content


# ---------------------------------------------------------------------------
# send_telegram_alert
# ---------------------------------------------------------------------------

class TestSendTelegram:
    def test_skips_without_creds(self, capsys):
        with mock.patch.dict(os.environ, {}, clear=True):
            heartbeat._send_telegram("test message")
        assert "WARN" in capsys.readouterr().out

    def test_sends_message(self):
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test",
            "ALLOWED_CHAT_ID": "123",
        }):
            with mock.patch("urllib.request.urlopen") as m:
                heartbeat._send_telegram("hello")
        m.assert_called_once()


class TestTelegramAlert:
    def test_sends_alert(self):
        checks = [
            {"name": "service:watcher", "ok": False, "detail": "NOT ACTIVE"},
        ]
        with mock.patch("heartbeat._send_telegram") as m:
            heartbeat.send_telegram_alert(checks)
        m.assert_called_once()
        msg = m.call_args[0][0]
        assert "UNHEALTHY" in msg
        assert "service:watcher" in msg


class TestTelegramHeartbeat:
    def test_healthy_pulse(self):
        checks = [
            {"name": "svc", "ok": True, "detail": "ok"},
            {"name": "disk", "ok": True, "detail": "ok"},
        ]
        with mock.patch("heartbeat._send_telegram") as m:
            heartbeat.send_telegram_heartbeat(checks)
        m.assert_called_once()
        msg = m.call_args[0][0]
        assert "HEALTHY" in msg
        assert "2/2" in msg

    def test_unhealthy_pulse(self):
        checks = [
            {"name": "svc", "ok": True, "detail": "ok"},
            {"name": "disk", "ok": False, "detail": "full"},
        ]
        with mock.patch("heartbeat._send_telegram") as m:
            heartbeat.send_telegram_heartbeat(checks)
        m.assert_called_once()
        msg = m.call_args[0][0]
        assert "UNHEALTHY" in msg
        assert "disk" in msg


# ---------------------------------------------------------------------------
# inject_repair_task
# ---------------------------------------------------------------------------

class TestInjectRepairTask:
    def test_injects_for_service_failure(self, tmp_path):
        _make_tmp_base(tmp_path)
        checks = [
            {"name": "service:novacore-watcher", "ok": False, "detail": "dead"},
            {"name": "disk", "ok": True, "detail": "fine"},
        ]
        heartbeat.inject_repair_task(checks)
        tasks = list((tmp_path / "TASKS").glob("hb_*_self_repair.md"))
        assert len(tasks) == 1
        assert "novacore-watcher" in tasks[0].read_text()

    def test_skips_non_service_failure(self, tmp_path):
        _make_tmp_base(tmp_path)
        checks = [
            {"name": "disk", "ok": False, "detail": "full"},
        ]
        heartbeat.inject_repair_task(checks)
        tasks = list((tmp_path / "TASKS").glob("hb_*"))
        assert len(tasks) == 0

    def test_rate_limits_existing_repair(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "TASKS" / "hb_20260305_old_self_repair.md.inprogress").write_text("x")
        checks = [
            {"name": "service:novacore-watcher", "ok": False, "detail": "dead"},
        ]
        heartbeat.inject_repair_task(checks)
        # Should not inject a second repair task
        new_tasks = list((tmp_path / "TASKS").glob("hb_*_self_repair.md"))
        assert len(new_tasks) == 0


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------

class TestMain:
    def test_healthy_run(self, tmp_path):
        _make_tmp_base(tmp_path)
        with mock.patch("heartbeat.check_service") as m_svc, \
             mock.patch("heartbeat.check_disk") as m_disk, \
             mock.patch("heartbeat.check_claude_binary") as m_claude, \
             mock.patch("heartbeat.send_telegram_heartbeat") as m_hb:
            m_svc.return_value = {"name": "svc", "ok": True, "detail": "ok"}
            m_disk.return_value = {"name": "disk", "ok": True, "detail": "ok"}
            m_claude.return_value = {"name": "claude", "ok": True, "detail": "ok"}
            code = heartbeat.main()
        assert code == 0
        assert heartbeat.HEARTBEAT_FILE.exists()
        assert (heartbeat.LOGS_DIR / "heartbeat.log").exists()
        m_hb.assert_called_once()

    def test_unhealthy_run(self, tmp_path):
        _make_tmp_base(tmp_path)
        with mock.patch("heartbeat.check_service") as m_svc, \
             mock.patch("heartbeat.check_disk") as m_disk, \
             mock.patch("heartbeat.check_claude_binary") as m_claude, \
             mock.patch("heartbeat.send_telegram_heartbeat") as m_hb, \
             mock.patch("heartbeat.inject_repair_task") as m_repair:
            m_svc.return_value = {"name": "svc", "ok": False, "detail": "dead"}
            m_disk.return_value = {"name": "disk", "ok": True, "detail": "ok"}
            m_claude.return_value = {"name": "claude", "ok": True, "detail": "ok"}
            code = heartbeat.main()
        assert code == 1
        m_hb.assert_called_once()
        m_repair.assert_called_once()
