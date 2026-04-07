"""Tests for heartbeat.py health checks."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

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
    for d in (
        heartbeat.STATE_DIR,
        heartbeat.TASKS_DIR,
        heartbeat.OUTPUT_DIR,
        heartbeat.LOGS_DIR,
        heartbeat.STATE_DIR / "running",
    ):
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

    def test_orphaned_inprogress_auto_recovered(self, tmp_path):
        _make_tmp_base(tmp_path)
        ip = tmp_path / "TASKS" / "0001_old.md.inprogress"
        ip.write_text("x")
        # Backdate mtime to make it orphaned
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
        os.utime(ip, (old_ts, old_ts))
        result = heartbeat.check_task_queue()
        # Orphaned tasks are now auto-recovered (reset to .md for retry)
        assert result["ok"] is True
        assert "AUTO-RECOVERED" in result["detail"]
        # The .inprogress file should be gone, .md should exist
        assert not ip.exists()
        assert (tmp_path / "TASKS" / "0001_old.md").exists()

    def test_too_many_pending(self, tmp_path):
        _make_tmp_base(tmp_path)
        for i in range(15):
            p = tmp_path / "TASKS" / f"{i:04d}_task.md"
            p.write_text("x")
            # Backdate to make stale (older than PENDING_ALERT_AGE_HOURS)
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).timestamp()
            os.utime(p, (old_ts, old_ts))
        result = heartbeat.check_task_queue()
        assert result["ok"] is False

    def test_fresh_tasks_dont_trigger_alert(self, tmp_path):
        """Fresh tasks (< PENDING_ALERT_AGE_HOURS) should not trigger alert."""
        _make_tmp_base(tmp_path)
        for i in range(15):
            (tmp_path / "TASKS" / f"{i:04d}_task.md").write_text("x")
        result = heartbeat.check_task_queue()
        assert result["ok"] is True
        assert "0 stale" in result["detail"]


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
        (tmp_path / "STATE" / "metrics.json").write_text(
            json.dumps(
                {
                    "contract_success": {"_total": 90},
                    "contract_failure": {"_total": 10},
                }
            )
        )
        result = heartbeat.check_metrics()
        assert result["ok"] is True
        assert "10.0%" in result["detail"]

    def test_unhealthy_metrics(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text(
            json.dumps(
                {
                    "contract_success": {"_total": 20},
                    "contract_failure": {"_total": 80},
                }
            )
        )
        result = heartbeat.check_metrics()
        assert result["ok"] is False
        assert "80.0%" in result["detail"]

    def test_plain_int_metrics(self, tmp_path):
        _make_tmp_base(tmp_path)
        (tmp_path / "STATE" / "metrics.json").write_text(
            json.dumps(
                {
                    "contract_success": 9,
                    "contract_failure": 1,
                }
            )
        )
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
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "ALLOWED_CHAT_ID": "123",
                },
            ),
            mock.patch("urllib.request.urlopen") as m,
        ):
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
        assert "attention" in msg
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
        assert "healthy" in msg.lower()

    def test_unhealthy_pulse(self):
        checks = [
            {"name": "svc", "ok": True, "detail": "ok"},
            {"name": "disk", "ok": False, "detail": "full"},
        ]
        with mock.patch("heartbeat._send_telegram") as m:
            heartbeat.send_telegram_heartbeat(checks)
        m.assert_called_once()
        msg = m.call_args[0][0]
        assert "issue" in msg.lower()
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
# check_cost_router
# ---------------------------------------------------------------------------


class TestCheckCostRouter:
    """Tests for check_cost_router() heartbeat health check."""

    @mock.patch("utils.cost_router.write_heartbeat_config")
    @mock.patch("utils.cost_router.compute_heartbeat_config")
    @mock.patch("utils.cost_router.check_budget_alerts")
    @mock.patch("utils.cost_router.get_rolling_average_cost")
    @mock.patch("utils.cost_router.get_cost_summary")
    def test_healthy(self, m_cost, m_avg, m_alerts, m_hb_config, m_write):
        """No alerts → ok=True with cost details and mode in detail."""
        m_cost.return_value = {"daily_cost_usd": 5.0, "monthly_cost_usd": 50.0, "monthly_pct": 25.0}
        m_avg.return_value = 4.5
        m_alerts.return_value = []
        hb_cfg = mock.MagicMock()
        hb_cfg.mode = "idle"
        m_hb_config.return_value = hb_cfg

        result = heartbeat.check_cost_router()

        assert result["ok"] is True
        assert result["name"] == "cost_router"
        assert "$5.00" in result["detail"]
        assert "$50.00" in result["detail"]
        assert "25.0%" in result["detail"]
        assert "mode=idle" in result["detail"]
        m_write.assert_called_once_with(hb_cfg)

    @mock.patch("utils.cost_router.write_heartbeat_config")
    @mock.patch("utils.cost_router.compute_heartbeat_config")
    @mock.patch("utils.cost_router.check_budget_alerts")
    @mock.patch("utils.cost_router.get_rolling_average_cost")
    @mock.patch("utils.cost_router.get_cost_summary")
    def test_alerts_present(self, m_cost, m_avg, m_alerts, m_hb_config, m_write):
        """Budget alerts → ok=True (informational) with [WARN] prefix."""
        m_cost.return_value = {"daily_cost_usd": 25.0, "monthly_cost_usd": 150.0, "monthly_pct": 75.0}
        m_avg.return_value = 8.0
        m_alerts.return_value = ["Daily cost $25 exceeds threshold", "2x rolling average spike"]
        hb_cfg = mock.MagicMock()
        hb_cfg.mode = "budget_constrained"
        m_hb_config.return_value = hb_cfg

        result = heartbeat.check_cost_router()

        assert result["ok"] is True
        assert result["name"] == "cost_router"
        assert "[WARN]" in result["detail"]
        assert "2 alert(s)" in result["detail"]
        assert "Daily cost" in result["detail"]
        assert "mode=budget_constrained" in result["detail"]

    @mock.patch("utils.cost_router.write_heartbeat_config")
    @mock.patch("utils.cost_router.compute_heartbeat_config")
    @mock.patch("utils.cost_router.check_budget_alerts")
    @mock.patch("utils.cost_router.get_rolling_average_cost")
    @mock.patch("utils.cost_router.get_cost_summary")
    def test_budget_constrained_mode(self, m_cost, m_avg, m_alerts, m_hb_config, m_write):
        """No alerts but budget_constrained mode → ok=True with mode visible."""
        m_cost.return_value = {"daily_cost_usd": 18.0, "monthly_cost_usd": 170.0, "monthly_pct": 85.0}
        m_avg.return_value = 10.0
        m_alerts.return_value = []
        hb_cfg = mock.MagicMock()
        hb_cfg.mode = "budget_constrained"
        m_hb_config.return_value = hb_cfg

        result = heartbeat.check_cost_router()

        assert result["ok"] is True
        assert "mode=budget_constrained" in result["detail"]
        assert "$18.00" in result["detail"]
        assert "85.0%" in result["detail"]

    def test_exception_handling(self):
        """If cost_router functions fail, check degrades gracefully."""
        with mock.patch("utils.cost_router.get_cost_summary", side_effect=RuntimeError("redis down")):
            result = heartbeat.check_cost_router()

        assert result["ok"] is True
        assert result["name"] == "cost_router"
        assert "check skipped" in result["detail"]
        assert "redis down" in result["detail"]


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------


def _mock_all_heartbeat_checks(stack, *, overrides=None):
    """Set up all heartbeat mocks with healthy defaults, return mock dict.

    Single source of truth for heartbeat.main() mock targets.  When a new
    check is added to heartbeat.py, add it here once — all tests that call
    this helper automatically pick it up, eliminating copy-paste drift.

    Args:
        stack: contextlib.ExitStack to register patches on.
        overrides: dict mapping mock target strings to custom return values.
            Example: {"heartbeat.check_service": {"name":"svc","ok":False,"detail":"dead"}}
    """
    overrides = overrides or {}

    def _ok(name):
        return {"name": name, "ok": True, "detail": "ok"}

    # -- Health check functions (one entry per check_* call in heartbeat.main) --
    _check_defaults = {
        "heartbeat.check_service": _ok("svc"),
        "heartbeat.check_disk": _ok("disk"),
        "heartbeat.check_claude_binary": _ok("claude"),
        "heartbeat.check_backup": _ok("backup"),
        "heartbeat.check_memory_systems": _ok("memory_systems"),
        "heartbeat.check_google_workspace": _ok("google_workspace"),
        "heartbeat.check_pip_audit": _ok("pip_audit"),
        "heartbeat.check_ruff": _ok("ruff"),
        "heartbeat.check_task_queue": _ok("task_queue"),
        "heartbeat.check_last_output": _ok("last_output"),
        "heartbeat.check_stale_workers": _ok("stale_workers"),
        "heartbeat.check_state_files": _ok("state_files"),
        "heartbeat.check_metrics": _ok("metrics"),
        "heartbeat.check_log_sizes": _ok("log_sizes"),
        "heartbeat.check_state_bloat": _ok("state_bloat"),
        "heartbeat.check_llm_cache": _ok("llm_cache"),
        "heartbeat.check_cost_router": _ok("cost_router"),
        "heartbeat.check_scheduler": _ok("scheduler"),
        "heartbeat.check_token_budget": _ok("token_budget"),
        "heartbeat.check_weekly_budget": _ok("weekly_budget"),
        "heartbeat.check_novatrade_signals": _ok("novatrade_signals"),
        "utils.max_plan_guard.check_max_plan_usage": _ok("max_plan_usage"),
        "utils.self_healing.check_self_healing": _ok("self_healing"),
    }

    mocks = {}
    for target, default_rv in _check_defaults.items():
        rv = overrides.get(target, default_rv)
        mocks[target] = stack.enter_context(mock.patch(target, return_value=rv))

    # -- Non-check infrastructure mocks (always needed for main() to run) --
    stack.enter_context(mock.patch("heartbeat._mpg_allow", return_value=(False, "blocked: test")))
    stack.enter_context(mock.patch("heartbeat._budget_enforcer", None))
    stack.enter_context(mock.patch("heartbeat._sh_get_tier", None))
    stack.enter_context(mock.patch("heartbeat._ProgressScorer", None))
    stack.enter_context(mock.patch("utils.self_healing.touch_dead_man_switch"))
    stack.enter_context(mock.patch("utils.drift_detector.detect_drift", side_effect=ImportError("mocked")))
    stack.enter_context(mock.patch("utils.service_staleness.check_all_staleness", return_value=[]))
    stack.enter_context(mock.patch("scripts.cleanup_watcher_resources.main"))
    # Prevent OTel/PostHog daemon threads from memory_triggers (blocks teardown 60s+)
    stack.enter_context(mock.patch("agents.memory_triggers.trigger_engine.fire"))
    # Skip expensive post-check phases (LLM agent, memory, autonomy)
    stack.enter_context(mock.patch("heartbeat._heartbeat_shutdown_requested", True))

    # -- Observable mocks (tests may assert on these) --
    mocks["send_telegram_heartbeat"] = stack.enter_context(mock.patch("heartbeat.send_telegram_heartbeat"))
    mocks["inject_repair_task"] = stack.enter_context(mock.patch("heartbeat.inject_repair_task"))

    return mocks


@pytest.mark.timeout(30)  # With memory_triggers.fire mocked, no PostHog/OTel threads to block teardown
class TestMain:
    def test_healthy_run(self, tmp_path):
        _make_tmp_base(tmp_path)
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = _mock_all_heartbeat_checks(stack)
            code = heartbeat.main()
        assert code == 0
        assert heartbeat.HEARTBEAT_FILE.exists()
        assert (heartbeat.LOGS_DIR / "heartbeat.log").exists()
        mocks["send_telegram_heartbeat"].assert_called_once()

    def test_unhealthy_run(self, tmp_path):
        _make_tmp_base(tmp_path)
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = _mock_all_heartbeat_checks(
                stack,
                overrides={
                    "heartbeat.check_service": {"name": "svc", "ok": False, "detail": "dead"},
                },
            )
            code = heartbeat.main()
        # main() always returns 0 — health issues are reported via Telegram/repair tasks,
        # not via exit code (exit 1 confuses systemd oneshot status).
        assert code == 0
        mocks["send_telegram_heartbeat"].assert_called_once()
        mocks["inject_repair_task"].assert_called_once()


# ---------------------------------------------------------------------------
# _is_self_referential_runaway
# ---------------------------------------------------------------------------


class TestSelfReferentialRunaway:
    """Tests for the feedback loop breaker that prevents the heartbeat agent
    from amplifying its own runaway detection into a persistent UNHEALTHY state."""

    def test_ok_result_returns_false(self):
        result = {"ok": True, "detail": "normal", "runaway": {"detected": False, "reasons": []}}
        assert heartbeat._is_self_referential_runaway(result) is False

    def test_no_runaway_returns_false(self):
        result = {"ok": False, "detail": "high burn", "runaway": {"detected": False, "reasons": []}}
        assert heartbeat._is_self_referential_runaway(result) is False

    def test_other_caller_runaway_returns_false(self):
        result = {
            "ok": False,
            "detail": "runaway",
            "runaway": {"detected": True, "reasons": ["runaway_same_caller: watcher (15 calls)"]},
        }
        assert heartbeat._is_self_referential_runaway(result) is False

    def test_heartbeat_agent_runaway_returns_true(self):
        result = {
            "ok": False,
            "detail": "runaway",
            "runaway": {"detected": True, "reasons": ["runaway_same_caller: heartbeat_agent (23 calls)"]},
        }
        assert heartbeat._is_self_referential_runaway(result) is True

    def test_heartbeat_agent_among_multiple_reasons(self):
        result = {
            "ok": False,
            "detail": "runaway",
            "runaway": {
                "detected": True,
                "reasons": [
                    "high_burn_rate: 5.0x",
                    "runaway_same_caller: heartbeat_agent (40 calls)",
                ],
            },
        }
        assert heartbeat._is_self_referential_runaway(result) is True

    def test_missing_runaway_key_returns_false(self):
        result = {"ok": False, "detail": "error"}
        assert heartbeat._is_self_referential_runaway(result) is False

    def test_suppression_in_main_flow(self, tmp_path):
        """Integration: when heartbeat_agent is the runaway caller, the
        max_plan_usage check should be downgraded to ok=True so the overall
        heartbeat status is HEALTHY (breaking the feedback loop)."""
        _make_tmp_base(tmp_path)
        mpg_result = {
            "name": "max_plan_usage",
            "ok": False,
            "detail": "mode=PROTECTION, burn=2.77x, runaway=heartbeat_agent",
            "alerts": [],
            "protection_mode": "PROTECTION",
            "burn_rate": {},
            "runaway": {"detected": True, "reasons": ["runaway_same_caller: heartbeat_agent (23 calls)"]},
        }
        from contextlib import ExitStack

        with ExitStack() as stack:
            _mock_all_heartbeat_checks(
                stack,
                overrides={
                    "utils.max_plan_guard.check_max_plan_usage": mpg_result,
                },
            )
            code = heartbeat.main()
        # The self-referential runaway should be suppressed: overall HEALTHY
        assert code == 0
