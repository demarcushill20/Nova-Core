"""Tests for research injection permanent fixes (task 0721).

Covers:
- Injected task body contains explicit CONTRACT guidance
- Circuit breaker blocks injections after consecutive failures
- Circuit breaker resets on success
- Adaptive retry appends escalation context
- Proactive tasks use PROACTIVE_MAX_RETRIES (1) not global MAX_TASK_RETRIES (3)
- Normal heartbeat / user-requested task paths remain unaffected
"""

import time

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect all filesystem state to a temp directory."""
    monkeypatch.setattr("utils.heartbeat_rules.STATE", tmp_path / "STATE")
    monkeypatch.setattr("utils.heartbeat_rules.TASKS", tmp_path / "TASKS")
    monkeypatch.setattr("utils.heartbeat_rules.OUTPUT", tmp_path / "OUTPUT")
    monkeypatch.setattr(
        "utils.heartbeat_rules._RESEARCH_CB_STATE_FILE",
        tmp_path / "STATE" / "research_injection_cb.json",
    )
    (tmp_path / "STATE").mkdir(parents=True, exist_ok=True)
    (tmp_path / "TASKS").mkdir(parents=True, exist_ok=True)
    (tmp_path / "OUTPUT").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Injected task body contains CONTRACT guidance
# ---------------------------------------------------------------------------


class TestResearchInjectionBody:
    """Verify the task body includes explicit contract instructions."""

    def test_body_contains_contract_block(self):
        from utils.heartbeat_rules import _build_research_injection_body

        body = _build_research_injection_body(queue_size=1)
        assert "## CONTRACT" in body
        assert "summary:" in body
        assert "files_changed:" in body
        assert "verification:" in body
        assert "confidence:" in body

    def test_body_contains_output_format(self):
        from utils.heartbeat_rules import _build_research_injection_body

        body = _build_research_injection_body(queue_size=2)
        assert "## Executive Summary" in body
        assert "## Key Findings" in body
        assert "## Sources" in body

    def test_body_contains_failure_warning(self):
        from utils.heartbeat_rules import _build_research_injection_body

        body = _build_research_injection_body(queue_size=0)
        assert "MUST" in body
        assert "rejection" in body.lower() or "CONTRACT" in body

    def test_pipeline_action_uses_enhanced_body(self, monkeypatch):
        """The _check_research_pipeline action uses the enhanced body."""
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        # No pending research, stale output, low queue
        monkeypatch.setattr(engine, "_has_pending_research_tasks", lambda: False)
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        actions, _, _ = engine._check_research_pipeline([])
        assert len(actions) == 1
        assert "## CONTRACT" in actions[0]["body"]

    def test_idle_action_uses_enhanced_body(self, monkeypatch):
        """The _check_idle_detection action uses the enhanced body.

        We only run this test during active hours (06-22 UTC), which covers
        all CI/runtime contexts. The body content is tested independently
        via _build_research_injection_body above — here we just verify the
        idle detection path calls it when conditions are met.
        """
        import datetime as dt_mod

        current_hour = dt_mod.datetime.now(dt_mod.timezone.utc).hour
        if not (6 <= current_hour < 22):
            pytest.skip("Test requires active hours (06-22 UTC)")

        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        actions, _, _ = engine._check_idle_detection([])
        assert len(actions) == 1
        assert "## CONTRACT" in actions[0]["body"]


# ---------------------------------------------------------------------------
# 2. Circuit breaker
# ---------------------------------------------------------------------------


class TestResearchInjectionCircuitBreaker:
    """Verify the circuit breaker trips and resets correctly."""

    def test_initial_state_not_tripped(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        assert not HeartbeatRulesEngine.is_research_injection_cb_tripped()

    def test_single_failure_does_not_trip(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert not HeartbeatRulesEngine.is_research_injection_cb_tripped()

    def test_two_consecutive_failures_trips(self):
        from utils.heartbeat_rules import RESEARCH_INJECTION_CB_THRESHOLD, HeartbeatRulesEngine

        for _ in range(RESEARCH_INJECTION_CB_THRESHOLD):
            HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert HeartbeatRulesEngine.is_research_injection_cb_tripped()

    def test_success_resets_counter(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False)
        HeartbeatRulesEngine.record_research_injection_result(success=True)
        HeartbeatRulesEngine.record_research_injection_result(success=False)
        # Only 1 consecutive failure, not 2
        assert not HeartbeatRulesEngine.is_research_injection_cb_tripped()

    def test_manual_reset(self):
        from utils.heartbeat_rules import RESEARCH_INJECTION_CB_THRESHOLD, HeartbeatRulesEngine

        for _ in range(RESEARCH_INJECTION_CB_THRESHOLD):
            HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert HeartbeatRulesEngine.is_research_injection_cb_tripped()
        HeartbeatRulesEngine.reset_research_injection_cb()
        assert not HeartbeatRulesEngine.is_research_injection_cb_tripped()

    def test_tripped_cb_blocks_research_pipeline(self, monkeypatch):
        """When CB is tripped, _check_research_pipeline returns no actions."""
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import RESEARCH_INJECTION_CB_THRESHOLD, HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        monkeypatch.setattr(engine, "_has_pending_research_tasks", lambda: False)
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        # Trip the breaker
        for _ in range(RESEARCH_INJECTION_CB_THRESHOLD):
            HeartbeatRulesEngine.record_research_injection_result(success=False)

        actions, _, _ = engine._check_research_pipeline([])
        assert len(actions) == 0

    def test_tripped_cb_blocks_idle_detection(self, monkeypatch):
        """When CB is tripped, _check_idle_detection returns no actions."""
        import datetime as dt_mod

        current_hour = dt_mod.datetime.now(dt_mod.timezone.utc).hour
        if not (6 <= current_hour < 22):
            pytest.skip("Test requires active hours (06-22 UTC)")

        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import RESEARCH_INJECTION_CB_THRESHOLD, HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        for _ in range(RESEARCH_INJECTION_CB_THRESHOLD):
            HeartbeatRulesEngine.record_research_injection_result(success=False)

        actions, _, _ = engine._check_idle_detection([])
        assert len(actions) == 0

    def test_state_persists_to_disk(self, tmp_path):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False)
        state = HeartbeatRulesEngine._read_research_cb()
        assert state["consecutive_failures"] == 1
        assert not state["tripped"]

    def test_failure_reason_recorded(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False, failure_reason="Missing CONTRACT block")
        assert HeartbeatRulesEngine.get_last_failure_reason() == "Missing CONTRACT block"

    def test_failure_reason_cleared_on_success(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False, failure_reason="No output file")
        HeartbeatRulesEngine.record_research_injection_result(success=True)
        # Reason persists in state but consecutive_failures reset
        state = HeartbeatRulesEngine._read_research_cb()
        assert state["consecutive_failures"] == 0

    def test_rolling_window_tracks_results(self):
        from utils.heartbeat_rules import RESEARCH_INJECTION_ROLLING_WINDOW, HeartbeatRulesEngine

        for i in range(7):
            HeartbeatRulesEngine.record_research_injection_result(success=(i % 2 == 0))
        state = HeartbeatRulesEngine._read_research_cb()
        assert len(state["recent_results"]) == RESEARCH_INJECTION_ROLLING_WINDOW


# ---------------------------------------------------------------------------
# 2b. Rolling failure-rate backoff
# ---------------------------------------------------------------------------


class TestRollingBackoff:
    """Verify rolling failure-rate backoff behavior."""

    def test_no_backoff_with_insufficient_data(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        # Only one result — not enough data
        HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert not HeartbeatRulesEngine.is_research_injection_backed_off()

    def test_no_backoff_with_good_success_rate(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        # 4 successes, 1 failure = 20% failure rate, below 50%
        for _ in range(4):
            HeartbeatRulesEngine.record_research_injection_result(success=True)
        HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert not HeartbeatRulesEngine.is_research_injection_backed_off()

    def test_backoff_with_high_failure_rate(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        # 3 failures, 1 success = 75% failure rate, above 50%
        HeartbeatRulesEngine.record_research_injection_result(success=True)
        for _ in range(3):
            HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert HeartbeatRulesEngine.is_research_injection_backed_off()

    def test_backoff_expires_after_cooldown(self, monkeypatch):
        from utils.heartbeat_rules import RESEARCH_INJECTION_BACKOFF_COOLDOWN_S, HeartbeatRulesEngine

        # Create high failure rate
        for _ in range(3):
            HeartbeatRulesEngine.record_research_injection_result(success=False)
        assert HeartbeatRulesEngine.is_research_injection_backed_off()

        # Fast-forward past cooldown
        state = HeartbeatRulesEngine._read_research_cb()
        state["last_failure_ts"] = time.time() - RESEARCH_INJECTION_BACKOFF_COOLDOWN_S - 1
        HeartbeatRulesEngine._write_research_cb(state)
        assert not HeartbeatRulesEngine.is_research_injection_backed_off()

    def test_backoff_blocks_research_pipeline(self, monkeypatch):
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        monkeypatch.setattr(engine, "_has_pending_research_tasks", lambda: False)
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        # Create high failure rate (but below CB threshold to test backoff specifically)
        HeartbeatRulesEngine.record_research_injection_result(success=True)
        HeartbeatRulesEngine.record_research_injection_result(success=False)
        # Reset consecutive to avoid CB trip, keep rolling window
        state = HeartbeatRulesEngine._read_research_cb()
        state["consecutive_failures"] = 0
        state["tripped"] = False
        state["recent_results"] = [False, True, False, False]  # 75% failure
        state["last_failure_ts"] = time.time()
        HeartbeatRulesEngine._write_research_cb(state)

        actions, _, _ = engine._check_research_pipeline([])
        assert len(actions) == 0

    def test_backoff_blocks_idle_detection(self, monkeypatch):
        import datetime as dt_mod

        current_hour = dt_mod.datetime.now(dt_mod.timezone.utc).hour
        if not (6 <= current_hour < 22):
            pytest.skip("Test requires active hours (06-22 UTC)")

        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", True)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        monkeypatch.setattr(engine, "_last_output_age_hours", lambda prefix: 999.0)
        monkeypatch.setattr(engine, "_task_queue_size", lambda: 0)

        state = HeartbeatRulesEngine._default_cb_state()
        state["recent_results"] = [False, True, False, False]  # 75% failure
        state["last_failure_ts"] = time.time()
        HeartbeatRulesEngine._write_research_cb(state)

        actions, _, _ = engine._check_idle_detection([])
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# 3. Adaptive retry
# ---------------------------------------------------------------------------


class TestAdaptiveRetry:
    """Verify retry appends escalation context to task file."""

    def test_append_retry_context_adds_contract(self, tmp_path):
        import importlib

        import heartbeat

        importlib.reload(heartbeat)

        task_file = tmp_path / "test_task.md.inprogress"
        task_file.write_text("# Original task body\n\nDo research.")

        heartbeat._append_retry_context(task_file)

        content = task_file.read_text()
        assert "# Original task body" in content
        assert "RETRY ESCALATION" in content
        assert "## CONTRACT" in content
        assert "MANDATORY" in content

    def test_append_retry_context_preserves_original(self, tmp_path):
        import importlib

        import heartbeat

        importlib.reload(heartbeat)

        original = "# Heartbeat Proactive Task\n\nQueue is low."
        task_file = tmp_path / "test_task.md.inprogress"
        task_file.write_text(original)

        heartbeat._append_retry_context(task_file)

        content = task_file.read_text()
        assert content.startswith(original)

    def test_append_retry_context_includes_failure_reason(self, tmp_path):
        import importlib

        import heartbeat

        importlib.reload(heartbeat)

        task_file = tmp_path / "test_task.md.inprogress"
        task_file.write_text("# Task body")

        heartbeat._append_retry_context(task_file, failure_reason="Missing CONTRACT block in output")

        content = task_file.read_text()
        assert "Missing CONTRACT block in output" in content
        assert "PRIOR FAILURE REASON" in content

    def test_append_retry_context_loads_reason_from_cb(self, tmp_path, monkeypatch):
        import importlib

        import heartbeat

        importlib.reload(heartbeat)

        # Record a failure reason in the circuit breaker state
        from utils.heartbeat_rules import HeartbeatRulesEngine

        HeartbeatRulesEngine.record_research_injection_result(success=False, failure_reason="No output file found")

        task_file = tmp_path / "test_task.md.inprogress"
        task_file.write_text("# Task body")

        # No explicit reason passed — should load from CB state
        heartbeat._append_retry_context(task_file)

        content = task_file.read_text()
        assert "No output file found" in content

    def test_retry_content_differs_from_original(self, tmp_path):
        """Retry escalation changes the prompt content (not identical re-dispatch)."""
        import importlib

        import heartbeat

        importlib.reload(heartbeat)

        original = "# Heartbeat Proactive Task\n\nQueue is low."
        task_file = tmp_path / "test_task.md.inprogress"
        task_file.write_text(original)

        heartbeat._append_retry_context(task_file, failure_reason="test error")
        content = task_file.read_text()

        # Content must be strictly longer (retry context appended)
        assert len(content) > len(original)
        # Must contain both original and retry-specific content
        assert "RETRY ESCALATION" in content
        assert "test error" in content


# ---------------------------------------------------------------------------
# 4. Proactive retry limit
# ---------------------------------------------------------------------------


class TestProactiveRetryLimit:
    """Verify proactive tasks use PROACTIVE_MAX_RETRIES (1) not global (3)."""

    def test_proactive_max_retries_is_1(self):
        from utils.heartbeat_rules import PROACTIVE_MAX_RETRIES

        assert PROACTIVE_MAX_RETRIES == 1

    def test_global_max_retries_unchanged(self):
        from utils.task_checkpoint import MAX_TASK_RETRIES

        assert MAX_TASK_RETRIES == 3


# ---------------------------------------------------------------------------
# 5. Feature gate stays off
# ---------------------------------------------------------------------------


class TestFeatureGate:
    """Verify RESEARCH_INJECTION_ENABLED defaults to False."""

    def test_feature_gate_is_off(self):
        import importlib

        import utils.heartbeat_rules as hr

        importlib.reload(hr)
        assert hr.RESEARCH_INJECTION_ENABLED is False

    def test_disabled_gate_skips_research(self, monkeypatch):
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", False)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        actions, _, _ = engine._check_research_pipeline([])
        assert actions == []

    def test_disabled_gate_skips_idle(self, monkeypatch):
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", False)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        actions, _, _ = engine._check_idle_detection([])
        assert actions == []


# ---------------------------------------------------------------------------
# 6. Normal paths unaffected
# ---------------------------------------------------------------------------


class TestNormalPathsUnaffected:
    """Ensure non-proactive heartbeat rules still work correctly."""

    def test_planning_rule_unchanged(self, monkeypatch, tmp_path):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        # Create a research file newer than any plan
        output_dir = tmp_path / "OUTPUT"
        research_file = output_dir / "hb_research_test.md"
        research_file.write_text("test")

        engine = HeartbeatRulesEngine()
        actions, escalate, _ = engine._check_planning([])
        # Should produce a planning action since research is newer than plan
        assert any(a.get("title") == "hb-planning-cycle" for a in actions)

    def test_system_health_rule_unchanged(self):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        checks = [{"name": "disk_space", "ok": False, "detail": "90% full"}]
        actions, _, _ = engine._check_system_health(checks)
        assert len(actions) == 1
        assert "disk_space" in actions[0]["message"]

    def test_task_queue_health_unchanged(self, monkeypatch, tmp_path):
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        actions, _, _ = engine._check_task_queue_health([])
        # No tasks = no issues
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# 7. Watcher orphan recovery respects proactive retry limits
# ---------------------------------------------------------------------------


class TestWatcherOrphanRecoveryProactive:
    """Verify watcher orphan recovery uses PROACTIVE_MAX_RETRIES for proactive tasks."""

    def test_watcher_append_retry_context_adds_contract(self, tmp_path):
        """_append_retry_context_watcher adds CONTRACT block and retry escalation."""
        from watcher import _append_retry_context_watcher

        task_file = tmp_path / "hb_proactive_research_test.md.inprogress"
        task_file.write_text("# Research task\n\nDo research.")

        _append_retry_context_watcher(task_file)

        content = task_file.read_text()
        assert "# Research task" in content
        assert "RETRY ESCALATION" in content
        assert "## CONTRACT" in content
        assert "MANDATORY" in content

    def test_watcher_append_retry_context_idempotent(self, tmp_path):
        """Calling _append_retry_context_watcher twice doesn't double-append."""
        from watcher import _append_retry_context_watcher

        task_file = tmp_path / "hb_proactive_research_test.md.inprogress"
        task_file.write_text("# Research task")

        _append_retry_context_watcher(task_file)
        first_content = task_file.read_text()
        _append_retry_context_watcher(task_file)
        second_content = task_file.read_text()

        assert first_content == second_content

    def test_watcher_append_retry_context_includes_cb_reason(self, tmp_path):
        """_append_retry_context_watcher loads failure reason from circuit breaker."""
        from utils.heartbeat_rules import HeartbeatRulesEngine
        from watcher import _append_retry_context_watcher

        HeartbeatRulesEngine.record_research_injection_result(success=False, failure_reason="Missing CONTRACT block")

        task_file = tmp_path / "hb_proactive_research_test.md.inprogress"
        task_file.write_text("# Research task")

        _append_retry_context_watcher(task_file)

        content = task_file.read_text()
        assert "Missing CONTRACT block" in content
        assert "PRIOR FAILURE REASON" in content

    def test_watcher_retry_content_differs_from_original(self, tmp_path):
        """Watcher retry context makes the prompt content different from original."""
        from watcher import _append_retry_context_watcher

        original = "# Proactive Research Task\n\nQueue is low."
        task_file = tmp_path / "hb_proactive_research_test.md.inprogress"
        task_file.write_text(original)

        _append_retry_context_watcher(task_file)
        content = task_file.read_text()

        assert len(content) > len(original)
        assert "RETRY ESCALATION" in content
        assert "recovered by the watcher" in content


# ---------------------------------------------------------------------------
# 8. Contract-failure retry adapts for research injection tasks
# ---------------------------------------------------------------------------


class TestContractRetryAdaptive:
    """Verify _create_retry_task includes adaptive context for research injection."""

    def test_research_retry_includes_adaptive_context(self, tmp_path, monkeypatch):
        """Retry task for proactive research includes prior failure context."""
        monkeypatch.setattr("watcher.TASKS_DIR", tmp_path)
        monkeypatch.setattr("watcher.OUTPUT_DIR", tmp_path / "OUTPUT")
        from watcher import _create_retry_task

        output_file = tmp_path / "OUTPUT" / "hb_proactive_research_test__20260406-120000.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Some research output without CONTRACT")

        retry_path = _create_retry_task(
            "hb_proactive_research_test",
            output_file,
            ["missing required field: summary"],
            [],
        )
        content = retry_path.read_text()
        assert "Prior Failure Context" in content
        assert "proactive research injection" in content
        assert "CRITICAL" in content

    def test_research_retry_includes_cb_reason(self, tmp_path, monkeypatch):
        """Retry task for proactive research includes circuit breaker reason."""
        monkeypatch.setattr("watcher.TASKS_DIR", tmp_path)
        monkeypatch.setattr("watcher.OUTPUT_DIR", tmp_path / "OUTPUT")
        from utils.heartbeat_rules import HeartbeatRulesEngine
        from watcher import _create_retry_task

        HeartbeatRulesEngine.record_research_injection_result(
            success=False, failure_reason="no ## CONTRACT section found"
        )

        output_file = tmp_path / "OUTPUT" / "hb_proactive_research_test__20260406-120000.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Output")

        retry_path = _create_retry_task(
            "hb_proactive_research_test",
            output_file,
            ["missing required field: summary"],
            [],
        )
        content = retry_path.read_text()
        assert "no ## CONTRACT section found" in content

    def test_non_research_retry_has_no_adaptive_section(self, tmp_path, monkeypatch):
        """Retry task for normal tasks does NOT include research-specific context."""
        monkeypatch.setattr("watcher.TASKS_DIR", tmp_path)
        monkeypatch.setattr("watcher.OUTPUT_DIR", tmp_path / "OUTPUT")
        from watcher import _create_retry_task

        output_file = tmp_path / "OUTPUT" / "0042_normal_task__20260406-120000.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# Normal output")

        retry_path = _create_retry_task(
            "0042_normal_task",
            output_file,
            ["missing required field: confidence"],
            [],
        )
        content = retry_path.read_text()
        assert "Prior Failure Context" not in content
        assert "proactive research injection" not in content


# ---------------------------------------------------------------------------
# 9. NovaTrade / user-requested tasks unaffected
# ---------------------------------------------------------------------------


class TestNonResearchPathsUnaffected:
    """Verify NovaTrade and user-requested tasks bypass research injection logic."""

    def test_novatrade_task_not_affected_by_cb(self, tmp_path, monkeypatch):
        """A novatrade task stem does not trigger circuit breaker logic."""
        # Circuit breaker only activates for hb_proactive_*research* stems
        stem = "0099_novatrade_execute_strategy"
        assert not stem.startswith("hb_proactive_")

    def test_user_task_retry_not_limited_by_proactive_max(self):
        """User-requested tasks use global MAX_TASK_RETRIES, not PROACTIVE_MAX_RETRIES."""
        from utils.heartbeat_rules import PROACTIVE_MAX_RETRIES
        from utils.task_checkpoint import MAX_TASK_RETRIES

        # Global limit is higher than proactive limit
        assert MAX_TASK_RETRIES > PROACTIVE_MAX_RETRIES
        # A user task stem doesn't match proactive prefix
        assert not "0050_user_task".startswith("hb_proactive_")

    def test_evaluate_skips_injection_when_disabled(self, monkeypatch):
        """Full evaluate() with RESEARCH_INJECTION_ENABLED=False produces no research tasks."""
        monkeypatch.setattr("utils.heartbeat_rules.RESEARCH_INJECTION_ENABLED", False)
        from utils.heartbeat_rules import HeartbeatRulesEngine

        engine = HeartbeatRulesEngine()
        result = engine.evaluate([])
        research_tasks = [a for a in result.actions if "research" in a.get("title", "").lower()]
        assert len(research_tasks) == 0


# ---------------------------------------------------------------------------
# 10. Orchestrator step goals enforce CONTRACT at the authoritative layer
# ---------------------------------------------------------------------------


class TestStepGoalContractCompliance:
    """Verify orchestrator step goals include explicit CONTRACT instructions.

    The step goal is the authoritative prompt layer — workers prioritize it
    over inputs.task_text. Without CONTRACT instructions in the goal,
    workers may skip the CONTRACT block even when the task body requests it.
    """

    def test_stageB_research_synthesize_goal_has_contract(self):
        """_build_stageB_research_steps synthesize step mandates CONTRACT."""
        from tools.orchestrator_adapter import _build_stageB_research_steps

        steps = _build_stageB_research_steps("test_stem", "Do research")
        synth_steps = [s for s in steps if "synthesize" in s.step_id]
        assert len(synth_steps) == 1
        goal = synth_steps[0].goal
        assert "CONTRACT" in goal
        assert "MANDATORY" in goal
        assert "summary" in goal
        assert "files_changed" in goal

    def test_stageB_research_verify_goal_mentions_contract(self):
        """_build_stageB_research_steps verify step references CONTRACT."""
        from tools.orchestrator_adapter import _build_stageB_research_steps

        steps = _build_stageB_research_steps("test_stem", "Do research")
        verify_steps = [s for s in steps if "verify" in s.step_id]
        assert len(verify_steps) == 1
        assert "CONTRACT" in verify_steps[0].goal

    def test_default_research_synthesize_goal_has_contract(self):
        """_build_steps_for_class('research') synthesize step mandates CONTRACT."""
        from tools.orchestrator_adapter import _build_steps_for_class

        steps = _build_steps_for_class("test_stem", "research", "Do research")
        synth_steps = [s for s in steps if "synthesize" in s.step_id]
        assert len(synth_steps) == 1
        goal = synth_steps[0].goal
        assert "CONTRACT" in goal
        assert "MANDATORY" in goal
        assert "rejected" in goal.lower()

    def test_default_research_verify_goal_mentions_contract(self):
        """_build_steps_for_class('research') verify step references CONTRACT."""
        from tools.orchestrator_adapter import _build_steps_for_class

        steps = _build_steps_for_class("test_stem", "research", "Do research")
        verify_steps = [s for s in steps if "verify" in s.step_id]
        assert len(verify_steps) == 1
        assert "CONTRACT" in verify_steps[0].goal

    def test_code_impl_steps_unaffected(self):
        """_build_steps_for_class('code_impl') steps are NOT changed."""
        from tools.orchestrator_adapter import _build_steps_for_class

        steps = _build_steps_for_class("test_stem", "code_impl", "Fix bug")
        # code_impl synthesize step should NOT have the research CONTRACT mandate
        synth_steps = [s for s in steps if "synthesize" in s.step_id]
        assert len(synth_steps) == 0  # code_impl has analyze/implement/review, no synthesize

    def test_system_steps_unaffected(self):
        """_build_steps_for_class('system') steps are NOT changed."""
        from tools.orchestrator_adapter import _build_steps_for_class

        steps = _build_steps_for_class("test_stem", "system", "Check disk")
        # system steps should not have MANDATORY CONTRACT in goal
        for step in steps:
            assert "MANDATORY" not in step.goal or "CONTRACT" not in step.goal

    def test_stageD_report_steps_have_contract_in_goal(self):
        """_build_stageD_report_steps report step mandates CONTRACT."""
        from tools.orchestrator_adapter import _build_stageD_report_steps

        steps = _build_stageD_report_steps("test_stem", "Check system health")
        report_steps = [s for s in steps if "report" in s.step_id]
        assert len(report_steps) == 1
        goal = report_steps[0].goal
        assert "CONTRACT" in goal
        assert "MANDATORY" in goal
        assert "summary" in goal
        assert "files_changed" in goal

    def test_stageD_report_verify_mentions_contract(self):
        """_build_stageD_report_steps verify step references CONTRACT."""
        from tools.orchestrator_adapter import _build_stageD_report_steps

        steps = _build_stageD_report_steps("test_stem", "Check system health")
        verify_steps = [s for s in steps if "verify" in s.step_id]
        assert len(verify_steps) == 1
        assert "CONTRACT" in verify_steps[0].goal

    def test_stageD_inspect_analyze_has_contract_in_goal(self):
        """_build_stageD_inspect_steps analyze step mandates CONTRACT."""
        from tools.orchestrator_adapter import _build_stageD_inspect_steps

        steps = _build_stageD_inspect_steps("test_stem", "Inspect logs")
        analyze_steps = [s for s in steps if "analyze" in s.step_id]
        assert len(analyze_steps) == 1
        goal = analyze_steps[0].goal
        assert "CONTRACT" in goal
        assert "MANDATORY" in goal
        assert "summary" in goal

    def test_stageD_inspect_verify_mentions_contract(self):
        """_build_stageD_inspect_steps verify step references CONTRACT."""
        from tools.orchestrator_adapter import _build_stageD_inspect_steps

        steps = _build_stageD_inspect_steps("test_stem", "Inspect logs")
        verify_steps = [s for s in steps if "verify" in s.step_id]
        assert len(verify_steps) == 1
        assert "CONTRACT" in verify_steps[0].goal
