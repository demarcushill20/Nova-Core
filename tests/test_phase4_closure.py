"""Phase 4 closure tests: prove cost-routed model reaches the subprocess command.

Review revision A: integration test showing cost_route_task().model flows
through _dispatch_inner() into the --model argument passed to _execute_worker().
"""

import os
import sys
from contextlib import ExitStack
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _setup_watcher_mocks(stack, tmp_path, fake_decision, fake_execute_worker, task_class="simple"):
    """Enter all mock.patch context managers via ExitStack to avoid nesting limit."""
    mock_dlp_result = mock.MagicMock(action="allow", findings=[])
    mock_session_mgr = mock.MagicMock()
    mock_session_mgr.build_context_injection.return_value = ""
    mock_session_mgr.get_or_create_session.return_value = mock.MagicMock(session_id="test-session")
    mock_routing = {
        "task_class": task_class,
        "confidence": 0.9,
        "use_orchestrator": False,
        "fallback_reason": "test",
        "feature_flags": {},
    }
    mock_budget_mod = mock.MagicMock()
    mock_budget_mod.budget.can_proceed.return_value = (True, "ok")

    from utils.async_worker_pool import TaskPriority

    patches = [
        mock.patch("watcher.check_kill_switch", return_value="run"),
        mock.patch.dict("sys.modules", {"agents.budget_enforcer": mock_budget_mod}),
        mock.patch("watcher.validate_task_content", return_value={"blocked": False, "risks": [], "risk_score": 0}),
        mock.patch("watcher.dlp", mock.MagicMock(scan=mock.MagicMock(return_value=mock_dlp_result))),
        mock.patch("watcher.classify_and_route", return_value=mock_routing),
        mock.patch("watcher.cost_route_task", return_value=fake_decision),
        mock.patch("watcher.parse_task_priority", return_value=TaskPriority.NORMAL),
        mock.patch("watcher.load_skills", return_value=[]),
        mock.patch("watcher.select_skills", return_value=[]),
        mock.patch("watcher._session_mgr", mock_session_mgr),
        mock.patch("watcher.memory_router", mock.MagicMock(recall=mock.MagicMock(side_effect=Exception("skip")))),
        mock.patch("watcher._execute_worker", new_callable=mock.AsyncMock, side_effect=fake_execute_worker),
        mock.patch("watcher.trace_llm_call"),
        mock.patch("watcher.verify_artifacts", return_value=(True, [])),
        mock.patch(
            "watcher._run_test_gate", new_callable=mock.AsyncMock, return_value={"status": "pass", "summary": "ok"}
        ),
        mock.patch("watcher._apply_test_gate_result", return_value=True),
        mock.patch("watcher.trigger_engine", mock.MagicMock()),
        mock.patch("watcher._audit", mock.MagicMock()),
        mock.patch("watcher.slog", mock.MagicMock()),
        mock.patch("watcher.audit_task_execution"),
        mock.patch("watcher._HAS_SCHEDULER", False),
    ]
    for p in patches:
        stack.enter_context(p)


class TestCostRoutedModelIntegration:
    """Prove cost_route_task().model flows end-to-end into the subprocess --model arg."""

    @pytest.mark.asyncio
    async def test_routed_model_sonnet_reaches_subprocess(self, tmp_path):
        """cost_route_task returns sonnet → cmd must contain --model claude-sonnet-4-20250514."""
        from utils.cost_router import RoutingDecision, TaskComplexity

        expected_model = "claude-sonnet-4-20250514"
        fake_decision = RoutingDecision(
            model=expected_model,
            complexity=TaskComplexity.SIMPLE,
            reasons=["test: forced sonnet"],
            estimated_tokens=500,
            budget_utilization_pct=10.0,
            downgraded=False,
        )

        # Temp dirs
        tasks_dir = tmp_path / "TASKS"
        for d in (
            tasks_dir,
            tmp_path / "OUTPUT",
            tmp_path / "LOGS",
            tmp_path / "WORK",
            tmp_path / "STATE" / "cancel",
            tmp_path / "STATE" / "running",
        ):
            d.mkdir(parents=True, exist_ok=True)

        task_file = tasks_dir / "0099_cost_test.md"
        task_file.write_text("# Test task\nFix a typo in README.", encoding="utf-8")

        # Capture cmd
        captured_cmds = []

        async def fake_execute_worker(
            stem, cmd, worker_log, selected_names, skill_flag_note, attempt, max_attempts, child_env=None, **kwargs
        ):
            captured_cmds.append(list(cmd))
            worker_log.write_text("fake output", encoding="utf-8")
            return 0

        import watcher

        saved = {
            "TASKS_DIR": watcher.TASKS_DIR,
            "OUTPUT_DIR": watcher.OUTPUT_DIR,
            "LOGS_DIR": watcher.LOGS_DIR,
            "WORK_DIR": watcher.WORK_DIR,
            "CANCEL_DIR": watcher.CANCEL_DIR,
            "RUNNING_DIR": watcher.RUNNING_DIR,
            "_dispatched": watcher._dispatched.copy(),
        }
        try:
            watcher.TASKS_DIR = tasks_dir
            watcher.OUTPUT_DIR = tmp_path / "OUTPUT"
            watcher.LOGS_DIR = tmp_path / "LOGS"
            watcher.WORK_DIR = tmp_path / "WORK"
            watcher.CANCEL_DIR = tmp_path / "STATE" / "cancel"
            watcher.RUNNING_DIR = tmp_path / "STATE" / "running"
            watcher._dispatched = set()

            with ExitStack() as stack:
                _setup_watcher_mocks(stack, tmp_path, fake_decision, fake_execute_worker)
                await watcher._dispatch_inner(task_file)

            # Verify
            assert len(captured_cmds) == 1, f"Expected 1 command, got {len(captured_cmds)}"
            cmd = captured_cmds[0]
            assert "--model" in cmd, f"--model not in command: {cmd}"
            model_idx = cmd.index("--model")
            actual_model = cmd[model_idx + 1]
            assert actual_model == expected_model, f"Expected --model {expected_model}, got --model {actual_model}"
        finally:
            watcher.TASKS_DIR = saved["TASKS_DIR"]
            watcher.OUTPUT_DIR = saved["OUTPUT_DIR"]
            watcher.LOGS_DIR = saved["LOGS_DIR"]
            watcher.WORK_DIR = saved["WORK_DIR"]
            watcher.CANCEL_DIR = saved["CANCEL_DIR"]
            watcher.RUNNING_DIR = saved["RUNNING_DIR"]
            watcher._dispatched = saved["_dispatched"]

    @pytest.mark.asyncio
    async def test_routed_model_opus_reaches_subprocess(self, tmp_path):
        """cost_route_task returns opus → cmd must contain --model claude-opus-4-8."""
        from utils.cost_router import RoutingDecision, TaskComplexity

        expected_model = "claude-opus-4-8"
        fake_decision = RoutingDecision(
            model=expected_model,
            complexity=TaskComplexity.COMPLEX,
            reasons=["test: forced opus"],
            estimated_tokens=8000,
            budget_utilization_pct=30.0,
            downgraded=False,
        )

        tasks_dir = tmp_path / "TASKS"
        for d in (
            tasks_dir,
            tmp_path / "OUTPUT",
            tmp_path / "LOGS",
            tmp_path / "WORK",
            tmp_path / "STATE" / "cancel",
            tmp_path / "STATE" / "running",
        ):
            d.mkdir(parents=True, exist_ok=True)

        task_file = tasks_dir / "0100_complex_task.md"
        task_file.write_text("# Complex\nRefactor the entire auth module.", encoding="utf-8")

        captured_cmds = []

        async def fake_execute_worker(
            stem, cmd, worker_log, selected_names, skill_flag_note, attempt, max_attempts, child_env=None, **kwargs
        ):
            captured_cmds.append(list(cmd))
            worker_log.write_text("fake output", encoding="utf-8")
            return 0

        import watcher

        saved = {
            "TASKS_DIR": watcher.TASKS_DIR,
            "OUTPUT_DIR": watcher.OUTPUT_DIR,
            "LOGS_DIR": watcher.LOGS_DIR,
            "WORK_DIR": watcher.WORK_DIR,
            "CANCEL_DIR": watcher.CANCEL_DIR,
            "RUNNING_DIR": watcher.RUNNING_DIR,
            "_dispatched": watcher._dispatched.copy(),
        }
        try:
            watcher.TASKS_DIR = tasks_dir
            watcher.OUTPUT_DIR = tmp_path / "OUTPUT"
            watcher.LOGS_DIR = tmp_path / "LOGS"
            watcher.WORK_DIR = tmp_path / "WORK"
            watcher.CANCEL_DIR = tmp_path / "STATE" / "cancel"
            watcher.RUNNING_DIR = tmp_path / "STATE" / "running"
            watcher._dispatched = set()

            with ExitStack() as stack:
                _setup_watcher_mocks(stack, tmp_path, fake_decision, fake_execute_worker, task_class="code_impl")
                await watcher._dispatch_inner(task_file)

            assert len(captured_cmds) == 1
            cmd = captured_cmds[0]
            model_idx = cmd.index("--model")
            assert cmd[model_idx + 1] == expected_model
        finally:
            watcher.TASKS_DIR = saved["TASKS_DIR"]
            watcher.OUTPUT_DIR = saved["OUTPUT_DIR"]
            watcher.LOGS_DIR = saved["LOGS_DIR"]
            watcher.WORK_DIR = saved["WORK_DIR"]
            watcher.CANCEL_DIR = saved["CANCEL_DIR"]
            watcher.RUNNING_DIR = saved["RUNNING_DIR"]
            watcher._dispatched = saved["_dispatched"]
