"""Tests for utils/max_plan_guard.py — Max-plan usage monitoring & protection."""

import json

import pytest

from utils.max_plan_guard import (
    AuthStatus,
    BurnRateMetrics,
    GuardConfig,
    ProtectionMode,
    ProtectionState,
    RunawayDetection,
    _recommend_action,
    _run_auth_check,
    check_auth_mode,
    check_max_plan_usage,
    compute_burn_rate,
    detect_runaway,
    evaluate_protection_mode,
    generate_alerts,
    load_protection_state,
    prune_ledger,
    read_ledger,
    record_invocation,
    save_protection_state,
    set_config,
    should_allow_task,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Redirect all STATE paths to tmp_path so tests don't touch production."""
    monkeypatch.setattr("utils.max_plan_guard.STATE_DIR", tmp_path)
    monkeypatch.setattr("utils.max_plan_guard.LEDGER_FILE", tmp_path / "max_plan_ledger.jsonl")
    monkeypatch.setattr("utils.max_plan_guard.STATUS_FILE", tmp_path / "max_plan_status.json")
    monkeypatch.setattr("utils.max_plan_guard.AUTH_CACHE_FILE", tmp_path / "max_plan_auth_cache.json")
    monkeypatch.setattr("utils.max_plan_guard.PROTECTION_STATE_FILE", tmp_path / "max_plan_protection.json")
    # Reset config to defaults
    set_config(GuardConfig())


# ---------------------------------------------------------------------------
# Auth mode detection
# ---------------------------------------------------------------------------
class TestAuthMode:
    def test_max_plan_detected(self, monkeypatch):
        """claude auth status returning max plan login is correctly classified."""
        auth_json = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "test@example.com",
            }
        )

        def fake_run(*args, **kwargs):
            class R:
                stdout = auth_json
                returncode = 0

            return R()

        monkeypatch.setattr("utils.max_plan_guard.subprocess.run", fake_run)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        status = _run_auth_check()
        assert status.auth_mode == "max_plan_login"
        assert status.logged_in is True
        assert status.subscription_type == "max"
        assert status.api_key_present is False

    def test_no_api_key_subscription_path(self, monkeypatch):
        """Without API key and with max subscription, mode is max_plan_login."""
        auth_json = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "user@test.com",
            }
        )

        def fake_run(*args, **kwargs):
            class R:
                stdout = auth_json

            return R()

        monkeypatch.setattr("utils.max_plan_guard.subprocess.run", fake_run)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        status = _run_auth_check()
        assert status.auth_mode == "max_plan_login"
        assert status.api_key_present is False

    def test_api_key_present(self, monkeypatch):
        """API key present should result in api_key_mode."""
        auth_json = json.dumps(
            {
                "loggedIn": False,
                "authMethod": "api_key",
                "subscriptionType": "unknown",
            }
        )

        def fake_run(*args, **kwargs):
            class R:
                stdout = auth_json

            return R()

        monkeypatch.setattr("utils.max_plan_guard.subprocess.run", fake_run)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")

        status = _run_auth_check()
        assert status.auth_mode == "api_key_mode"
        assert status.api_key_present is True

    def test_auth_check_failure_graceful(self, monkeypatch):
        """Auth check failure returns unknown mode, not crash."""

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr("utils.max_plan_guard.subprocess.run", fake_run)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        status = _run_auth_check()
        assert status.auth_mode == "unknown"
        assert "claude not found" in status.error

    def test_auth_cache_works(self, monkeypatch, tmp_path):
        """Auth result is cached and reused within TTL."""
        call_count = 0

        def fake_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1

            class R:
                stdout = json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "subscriptionType": "max",
                        "email": "x@x.com",
                    }
                )

            return R()

        monkeypatch.setattr("utils.max_plan_guard.subprocess.run", fake_run)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # First call: hits subprocess
        s1 = check_auth_mode(force=False)
        assert call_count == 1
        assert s1.auth_mode == "max_plan_login"

        # Second call: should use cache
        check_auth_mode(force=False)
        assert call_count == 1  # no additional subprocess call

        # Force: bypasses cache
        check_auth_mode(force=True)
        assert call_count == 2

    def test_auth_mode_drift_alert(self):
        """Auth mode change from max_plan_login triggers alert."""
        auth = AuthStatus(
            auth_mode="api_key_mode",
            logged_in=False,
            subscription_type="unknown",
            email="",
            api_key_present=True,
        )
        burn = BurnRateMetrics()
        runaway = RunawayDetection()
        protection = ProtectionState()

        alerts = generate_alerts(auth, burn, runaway, protection)
        auth_alerts = [a for a in alerts if "Auth mode drift" in a.title]
        assert len(auth_alerts) == 1
        assert auth_alerts[0].severity == "warning"


# ---------------------------------------------------------------------------
# Invocation ledger
# ---------------------------------------------------------------------------
class TestLedger:
    def test_record_and_read(self, tmp_path):
        """Record an invocation and read it back."""
        rec = record_invocation(
            caller="watcher",
            component="watcher._execute_worker",
            task_id="test_task_1",
            model="claude-opus-4-6",
        )
        assert rec.caller == "watcher"
        assert rec.task_id == "test_task_1"

        entries = read_ledger(max_age_hours=1.0)
        assert len(entries) == 1
        assert entries[0]["caller"] == "watcher"

    def test_multiple_records_append(self, tmp_path):
        """Multiple records are appended, not overwritten."""
        record_invocation(caller="watcher", component="w.a", task_id="t1")
        record_invocation(caller="heartbeat", component="h.b", task_id="t2")
        record_invocation(caller="telegram", component="t.c", task_id="t3")

        entries = read_ledger()
        assert len(entries) == 3

    def test_old_entries_filtered(self, monkeypatch, tmp_path):
        """read_ledger filters entries by age."""
        # Write an entry with old timestamp
        old_ts = "2020-01-01T00:00:00+00:00"
        ledger_path = tmp_path / "max_plan_ledger.jsonl"
        with open(ledger_path, "w") as f:
            f.write(json.dumps({"timestamp": old_ts, "caller": "old"}) + "\n")

        # Write a recent entry
        record_invocation(caller="recent", component="r", task_id="r1")

        entries = read_ledger(max_age_hours=1.0)
        assert len(entries) == 1
        assert entries[0]["caller"] == "recent"

    def test_prune_ledger(self, tmp_path):
        """Prune removes old entries."""
        ledger_path = tmp_path / "max_plan_ledger.jsonl"
        old_ts = "2020-01-01T00:00:00+00:00"
        with open(ledger_path, "w") as f:
            f.write(json.dumps({"timestamp": old_ts, "caller": "old"}) + "\n")
        record_invocation(caller="recent", component="r", task_id="r1")

        removed = prune_ledger(max_age_hours=1.0)
        assert removed == 1
        entries = read_ledger()
        assert len(entries) == 1
        assert entries[0]["caller"] == "recent"


# ---------------------------------------------------------------------------
# Burn-rate analysis
# ---------------------------------------------------------------------------
class TestBurnRate:
    def test_empty_ledger(self):
        """Empty ledger produces zero metrics."""
        metrics = compute_burn_rate()
        assert metrics.claude_calls_last_15m == 0
        assert metrics.claude_calls_last_60m == 0
        assert metrics.burn_rate_multiplier == 0.0

    def test_spike_detection(self, tmp_path):
        """Many recent calls produce elevated burn rate."""
        # Record 20 calls in the last "minute"
        for i in range(20):
            record_invocation(caller="watcher", component="w", task_id=f"t{i}")

        metrics = compute_burn_rate()
        assert metrics.claude_calls_last_15m == 20
        assert metrics.claude_calls_last_60m == 20
        assert metrics.dominant_caller_last_60m == "watcher"
        assert metrics.dominant_caller_count == 20

    def test_same_caller_dominance(self, tmp_path):
        """Dominant caller is correctly identified."""
        for _ in range(8):
            record_invocation(caller="heartbeat_agent", component="h", task_id="")
        for _ in range(3):
            record_invocation(caller="watcher", component="w", task_id="")

        metrics = compute_burn_rate()
        assert metrics.dominant_caller_last_60m == "heartbeat_agent"
        assert metrics.dominant_caller_count == 8
        assert metrics.unique_callers_last_60m == 2

    def test_anomaly_score_scales(self, tmp_path):
        """Anomaly score increases with more calls."""
        for i in range(30):
            record_invocation(caller="watcher", component="w", task_id=f"t{i}")

        metrics = compute_burn_rate()
        assert metrics.anomaly_score > 0.0


# ---------------------------------------------------------------------------
# Runaway loop detection
# ---------------------------------------------------------------------------
class TestRunawayDetection:
    def test_no_runaway_empty_ledger(self):
        """Empty ledger = no runaway."""
        result = detect_runaway()
        assert result.detected is False
        assert result.reasons == []

    def test_same_caller_runaway(self, tmp_path):
        """Same caller exceeding threshold triggers detection."""
        cfg = GuardConfig(same_caller_threshold_60m=5)
        set_config(cfg)

        for _ in range(6):
            record_invocation(caller="heartbeat_agent", component="h", task_id="")

        result = detect_runaway()
        assert result.detected is True
        assert any("runaway_same_caller" in r for r in result.reasons)

    def test_same_task_runaway(self, tmp_path):
        """Same task exceeding threshold triggers detection."""
        cfg = GuardConfig(same_task_threshold_60m=3)
        set_config(cfg)

        for _ in range(4):
            record_invocation(caller="watcher", component="w", task_id="stuck_task")

        result = detect_runaway()
        assert result.detected is True
        assert any("runaway_same_task" in r for r in result.reasons)

    def test_failure_loop_detection(self, tmp_path):
        """Repeated failures from same caller triggers detection."""
        cfg = GuardConfig(failure_retry_threshold_60m=3)
        set_config(cfg)

        for _ in range(4):
            record_invocation(caller="watcher", component="w", task_id="failing_task", success=False)

        result = detect_runaway()
        assert result.detected is True
        assert any("runaway_failure_retry_loop" in r for r in result.reasons)
        assert result.severity == "critical"

    def test_heartbeat_loop_detection(self, tmp_path):
        """Excessive heartbeat-family calls triggers detection."""
        for _ in range(7):
            record_invocation(caller="heartbeat_agent", component="h", task_id="")

        result = detect_runaway()
        assert result.detected is True
        assert any("runaway_heartbeat_loop" in r for r in result.reasons)

    def test_abnormal_spike_detection(self, tmp_path):
        """Large number of calls triggers abnormal spike."""
        cfg = GuardConfig(protection_calls_60m=10, critical_calls_60m=20)
        set_config(cfg)

        for i in range(25):
            record_invocation(caller=f"caller_{i % 5}", component="c", task_id=f"task_{i}")

        result = detect_runaway()
        assert result.detected is True
        assert any("abnormal_usage_spike" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Protection mode transitions
# ---------------------------------------------------------------------------
class TestProtectionMode:
    def _make_auth(self, mode="max_plan_login"):
        return AuthStatus(
            auth_mode=mode,
            logged_in=True,
            subscription_type="max",
            email="test@test.com",
            api_key_present=False,
        )

    def test_normal_mode_default(self):
        """Default state is NORMAL."""
        burn = BurnRateMetrics()
        runaway = RunawayDetection()
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.NORMAL

    def test_caution_on_burn_rate(self, tmp_path):
        """Elevated burn rate triggers CAUTION."""
        burn = BurnRateMetrics(burn_rate_multiplier=2.5, claude_calls_last_60m=5)
        runaway = RunawayDetection()
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.CAUTION

    def test_protection_on_high_burn(self, tmp_path):
        """High burn rate triggers PROTECTION."""
        burn = BurnRateMetrics(burn_rate_multiplier=5.0, claude_calls_last_60m=5)
        runaway = RunawayDetection()
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.PROTECTION

    def test_protection_on_runaway_warning(self, tmp_path):
        """Runaway warning triggers PROTECTION."""
        burn = BurnRateMetrics()
        runaway = RunawayDetection(detected=True, reasons=["runaway_same_caller: test"], severity="warning")
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.PROTECTION

    def test_critical_lockdown_on_extreme_burn(self, tmp_path):
        """Extreme burn rate triggers CRITICAL_LOCKDOWN."""
        burn = BurnRateMetrics(burn_rate_multiplier=10.0, claude_calls_last_60m=5)
        runaway = RunawayDetection()
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.CRITICAL_LOCKDOWN

    def test_critical_lockdown_on_calls_threshold(self, tmp_path):
        """Extreme call count triggers CRITICAL_LOCKDOWN."""
        burn = BurnRateMetrics(claude_calls_last_60m=45)
        runaway = RunawayDetection()
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.CRITICAL_LOCKDOWN

    def test_critical_lockdown_on_critical_runaway(self, tmp_path):
        """Critical runaway triggers CRITICAL_LOCKDOWN."""
        burn = BurnRateMetrics()
        runaway = RunawayDetection(detected=True, reasons=["runaway_failure_retry_loop: test"], severity="critical")
        state = evaluate_protection_mode(burn, runaway, self._make_auth())
        assert state.mode == ProtectionMode.CRITICAL_LOCKDOWN

    def test_mode_persistence(self, tmp_path):
        """Protection state survives save/load cycle."""
        state = ProtectionState(
            mode=ProtectionMode.PROTECTION,
            reason="test reason",
            activated_at="2026-01-01T00:00:00",
        )
        save_protection_state(state)
        loaded = load_protection_state()
        assert loaded.mode == ProtectionMode.PROTECTION
        assert loaded.reason == "test reason"

    def test_escalation_recorded_in_history(self, tmp_path):
        """Mode escalation is recorded in history."""
        # First evaluation: normal
        burn0 = BurnRateMetrics()
        runaway0 = RunawayDetection()
        evaluate_protection_mode(burn0, runaway0, self._make_auth())

        # Second evaluation: spike → caution
        burn1 = BurnRateMetrics(burn_rate_multiplier=3.0)
        state = evaluate_protection_mode(burn1, runaway0, self._make_auth())
        assert state.mode == ProtectionMode.CAUTION
        assert len(state.history) >= 1
        assert state.history[-1]["to"] == "CAUTION"


# ---------------------------------------------------------------------------
# Task gating (should_allow_task)
# ---------------------------------------------------------------------------
class TestShouldAllowTask:
    def test_normal_allows_everything(self, tmp_path):
        """NORMAL mode allows all tasks."""
        save_protection_state(ProtectionState(mode=ProtectionMode.NORMAL))
        allowed, reason = should_allow_task("research_cycle")
        assert allowed is True

    def test_protection_blocks_research(self, tmp_path):
        """PROTECTION mode blocks non-essential callers."""
        save_protection_state(ProtectionState(mode=ProtectionMode.PROTECTION, reason="burn_rate_high"))
        allowed, reason = should_allow_task("research_cycle")
        assert allowed is False
        assert "blocked" in reason

    def test_protection_blocks_planning(self, tmp_path):
        """PROTECTION mode blocks planning."""
        save_protection_state(ProtectionState(mode=ProtectionMode.PROTECTION, reason="test"))
        allowed, reason = should_allow_task("planning_cycle")
        assert allowed is False

    def test_protection_allows_watcher(self, tmp_path):
        """PROTECTION mode allows essential callers like watcher."""
        save_protection_state(ProtectionState(mode=ProtectionMode.PROTECTION, reason="test"))
        allowed, reason = should_allow_task("watcher")
        assert allowed is True

    def test_critical_blocks_watcher(self, tmp_path):
        """CRITICAL_LOCKDOWN blocks even watcher."""
        save_protection_state(ProtectionState(mode=ProtectionMode.CRITICAL_LOCKDOWN, reason="extreme"))
        allowed, reason = should_allow_task("watcher")
        assert allowed is False

    def test_critical_allows_heartbeat(self, tmp_path):
        """CRITICAL_LOCKDOWN allows heartbeat agent (essential health)."""
        save_protection_state(ProtectionState(mode=ProtectionMode.CRITICAL_LOCKDOWN, reason="extreme"))
        allowed, reason = should_allow_task("heartbeat_agent")
        assert allowed is True

    def test_operator_always_allowed(self, tmp_path):
        """Operator-initiated tasks bypass all protection."""
        save_protection_state(ProtectionState(mode=ProtectionMode.CRITICAL_LOCKDOWN, reason="extreme"))
        allowed, reason = should_allow_task("watcher", is_operator=True)
        assert allowed is True

    def test_essential_always_allowed(self, tmp_path):
        """Essential tasks bypass all protection."""
        save_protection_state(ProtectionState(mode=ProtectionMode.CRITICAL_LOCKDOWN, reason="extreme"))
        allowed, reason = should_allow_task("anything", is_essential=True)
        assert allowed is True


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------
class TestAlerts:
    def _make_auth(self, mode="max_plan_login"):
        return AuthStatus(
            auth_mode=mode,
            logged_in=True,
            subscription_type="max",
            email="",
            api_key_present=False,
        )

    def test_no_alerts_normal(self):
        """Healthy state produces no alerts."""
        alerts = generate_alerts(
            self._make_auth(),
            BurnRateMetrics(),
            RunawayDetection(),
            ProtectionState(),
        )
        assert len(alerts) == 0

    def test_burn_rate_alert_levels(self):
        """Different burn rates produce different severity alerts."""
        auth = self._make_auth()
        runaway = RunawayDetection()
        prot = ProtectionState()

        # Elevated
        alerts = generate_alerts(auth, BurnRateMetrics(burn_rate_multiplier=2.5), runaway, prot)
        assert any(a.severity == "info" and "Elevated" in a.title for a in alerts)

        # High
        alerts = generate_alerts(auth, BurnRateMetrics(burn_rate_multiplier=5.0), runaway, prot)
        assert any(a.severity == "warning" and "High" in a.title for a in alerts)

        # Critical
        alerts = generate_alerts(auth, BurnRateMetrics(burn_rate_multiplier=9.0), runaway, prot)
        assert any(a.severity == "critical" and "Critical" in a.title for a in alerts)

    def test_runaway_alert(self):
        """Runaway detection produces an alert."""
        alerts = generate_alerts(
            self._make_auth(),
            BurnRateMetrics(),
            RunawayDetection(detected=True, reasons=["runaway_same_caller: test"], severity="warning"),
            ProtectionState(),
        )
        assert any("Runaway" in a.title for a in alerts)

    def test_mode_transition_alert(self):
        """Protection mode change produces an alert."""
        alerts = generate_alerts(
            self._make_auth(),
            BurnRateMetrics(),
            RunawayDetection(),
            ProtectionState(mode=ProtectionMode.PROTECTION, reason="test"),
            prev_protection_mode=ProtectionMode.NORMAL,
        )
        assert any("ESCALATED" in a.title for a in alerts)

    def test_mode_deescalation_alert(self):
        """Protection mode step-down produces info alert."""
        alerts = generate_alerts(
            self._make_auth(),
            BurnRateMetrics(),
            RunawayDetection(),
            ProtectionState(mode=ProtectionMode.NORMAL, reason="all clear"),
            prev_protection_mode=ProtectionMode.CAUTION,
        )
        assert any("DE-ESCALATED" in a.title for a in alerts)


# ---------------------------------------------------------------------------
# Low-allocation hint / usage warning parsing
# ---------------------------------------------------------------------------
class TestUsageHintParsing:
    def test_auth_error_produces_info_alert(self):
        """Auth check failure produces informational alert, not crash."""
        auth = AuthStatus(
            auth_mode="unknown",
            logged_in=False,
            subscription_type="unknown",
            email="",
            api_key_present=False,
            error="timeout",
        )
        alerts = generate_alerts(auth, BurnRateMetrics(), RunawayDetection(), ProtectionState())
        assert any("Auth check failed" in a.title for a in alerts)


# ---------------------------------------------------------------------------
# Heartbeat integration
# ---------------------------------------------------------------------------
class TestHeartbeatIntegration:
    def test_check_max_plan_usage_healthy(self, monkeypatch, tmp_path):
        """check_max_plan_usage returns ok=True when healthy."""

        def fake_auth(*a, **kw):
            return AuthStatus(
                auth_mode="max_plan_login",
                logged_in=True,
                subscription_type="max",
                email="x@x.com",
                api_key_present=False,
            )

        monkeypatch.setattr("utils.max_plan_guard.check_auth_mode", fake_auth)

        result = check_max_plan_usage()
        assert result["name"] == "max_plan_usage"
        assert result["ok"] is True
        assert "mode=NORMAL" in result["detail"]

    def test_check_max_plan_usage_unhealthy(self, monkeypatch, tmp_path):
        """check_max_plan_usage returns ok=False when critical."""

        def fake_auth(*a, **kw):
            return AuthStatus(
                auth_mode="max_plan_login",
                logged_in=True,
                subscription_type="max",
                email="x@x.com",
                api_key_present=False,
            )

        monkeypatch.setattr("utils.max_plan_guard.check_auth_mode", fake_auth)

        # Flood ledger to trigger critical
        for i in range(50):
            record_invocation(caller="watcher", component="w", task_id=f"t{i}")

        result = check_max_plan_usage()
        assert result["ok"] is False
        assert (
            "CRITICAL_LOCKDOWN" in result.get("protection_mode", "")
            or "critical" in result["detail"].lower()
            or result["ok"] is False
        )

    def test_check_max_plan_usage_exception_safe(self, monkeypatch, tmp_path):
        """check_max_plan_usage gracefully handles exceptions."""

        def boom(*a, **kw):
            raise RuntimeError("test explosion")

        monkeypatch.setattr("utils.max_plan_guard.check_auth_mode", boom)

        result = check_max_plan_usage()
        assert result["ok"] is True
        assert "check skipped" in result["detail"]

    def test_heartbeat_summary_fields(self, monkeypatch, tmp_path):
        """Heartbeat result includes required fields."""

        def fake_auth(*a, **kw):
            return AuthStatus(
                auth_mode="max_plan_login",
                logged_in=True,
                subscription_type="max",
                email="",
                api_key_present=False,
            )

        monkeypatch.setattr("utils.max_plan_guard.check_auth_mode", fake_auth)

        result = check_max_plan_usage()
        assert "protection_mode" in result
        assert "burn_rate" in result
        assert "runaway" in result
        assert "alerts" in result
        # Verify burn_rate sub-fields
        br = result["burn_rate"]
        assert "claude_calls_last_15m" in br
        assert "claude_calls_last_60m" in br
        assert "claude_calls_last_6h" in br
        assert "burn_rate_multiplier" in br
        assert "dominant_caller_last_60m" in br


# ---------------------------------------------------------------------------
# Operator status artifact
# ---------------------------------------------------------------------------
class TestOperatorStatus:
    def test_status_file_written(self, monkeypatch, tmp_path):
        """check_max_plan_usage writes operator status file."""

        def fake_auth(*a, **kw):
            return AuthStatus(
                auth_mode="max_plan_login",
                logged_in=True,
                subscription_type="max",
                email="",
                api_key_present=False,
            )

        monkeypatch.setattr("utils.max_plan_guard.check_auth_mode", fake_auth)

        check_max_plan_usage()
        status_file = tmp_path / "max_plan_status.json"
        assert status_file.exists()

        data = json.loads(status_file.read_text())
        assert "current_mode" in data
        assert "auth_mode" in data
        assert "burn_rate" in data
        assert "recommended_action" in data
        assert "top_callers_24h" in data

    def test_recommend_action_varies(self):
        """Recommendations change with protection mode."""
        prot_normal = ProtectionState(mode=ProtectionMode.NORMAL)
        assert "No action" in _recommend_action(prot_normal, [])

        prot_critical = ProtectionState(mode=ProtectionMode.CRITICAL_LOCKDOWN)
        assert "URGENT" in _recommend_action(prot_critical, [])


# ---------------------------------------------------------------------------
# Telegram cooldown integration
# ---------------------------------------------------------------------------
class TestTelegramIntegration:
    def test_cooldown_prevents_repeated_usage_alert(self, monkeypatch, tmp_path):
        """Same usage alert message is suppressed by cooldown."""
        # Import the heartbeat cooldown gate
        from heartbeat import _telegram_cooldown_gate

        # Redirect cooldown file to tmp
        monkeypatch.setattr("heartbeat.TELEGRAM_COOLDOWN_FILE", tmp_path / "tg_cooldown.json")

        msg = "⚡ Max-Plan Guard [WARNING]: High burn rate\n5.0x baseline"
        assert _telegram_cooldown_gate(msg) is True  # first: allowed
        assert _telegram_cooldown_gate(msg) is False  # second: suppressed

    def test_different_alert_goes_through(self, monkeypatch, tmp_path):
        """Materially different alert message passes cooldown."""
        from heartbeat import _telegram_cooldown_gate

        monkeypatch.setattr("heartbeat.TELEGRAM_COOLDOWN_FILE", tmp_path / "tg_cooldown.json")

        msg1 = "⚡ Max-Plan Guard [WARNING]: High burn rate\n5.0x baseline"
        msg2 = "⚡ Max-Plan Guard [CRITICAL]: Runaway loop detected\nsame_caller: watcher"
        assert _telegram_cooldown_gate(msg1) is True
        assert _telegram_cooldown_gate(msg2) is True  # different message goes through
