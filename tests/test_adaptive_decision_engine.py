"""Comprehensive tests for Phase 2: Adaptive Decision Engine."""

from __future__ import annotations

import json

import pytest

from novatrade.autonomy.decision_context import (
    ContextAssembler,
    DecisionContext,
    GoalSummary,
    TaskSummary,
)
from novatrade.autonomy.decision_engine import (
    ActionMode,
    Decision,
    DecisionConfig,
    DecisionEngine,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    SubMetric,
)
from novatrade.autonomy.task_generator import TaskSpec, TaskSpecGenerator

# =====================================================================
# Helper factories
# =====================================================================


def make_dimension(
    name: str, score: float, warnings: list[str] | None = None, sub_metrics: list[SubMetric] | None = None
) -> DimensionScore:
    sms = sub_metrics or [SubMetric(name="m1", value=score)]
    return DimensionScore(name=name, score=score, sub_metrics=sms, warnings=warnings or [])


def make_trend(current: float, avg_6h: float | None = None, direction: str = "stable") -> ScoreTrend:
    return ScoreTrend(current=current, avg_6h=avg_6h, direction=direction)


def make_report(
    dims: dict[str, float] | None = None,
    trends: dict[str, tuple[float, str]] | None = None,
    warnings_map: dict[str, list[str]] | None = None,
    sub_metrics_map: dict[str, list[SubMetric]] | None = None,
) -> ProgressReport:
    """Build a ProgressReport from dimension name->score mapping."""
    dims = dims or {
        "system_health": 80.0,
        "execution_pipeline": 75.0,
        "strategy_validity": 70.0,
        "risk_engine": 85.0,
        "performance_stability": 60.0,
    }
    warnings_map = warnings_map or {}
    sub_metrics_map = sub_metrics_map or {}
    trend_data = trends or {}

    dimensions = {}
    trend_objs = {}
    for name, score in dims.items():
        sms = sub_metrics_map.get(name, [SubMetric(name="default", value=score)])
        dimensions[name] = make_dimension(name, score, warnings=warnings_map.get(name, []), sub_metrics=sms)
        t_info = trend_data.get(name, (score, "stable"))
        trend_objs[name] = make_trend(current=score, avg_6h=t_info[0], direction=t_info[1])

    overall = sum(dims.values()) / len(dims) if dims else 0.0
    return ProgressReport(
        overall_score=min(100.0, max(0.0, overall)),
        dimensions=dimensions,
        trends=trend_objs,
    )


def make_context(
    report: ProgressReport | None = None,
    recent_decisions: list[dict] | None = None,
    pending_tasks: list[TaskSummary] | None = None,
) -> DecisionContext:
    return DecisionContext(
        progress_report=report or make_report(),
        recent_decisions=recent_decisions or [],
        pending_tasks=pending_tasks or [],
    )


# =====================================================================
# TaskSummary / GoalSummary schema tests
# =====================================================================


def test_task_summary_basic():
    t = TaskSummary(stem="0001_test", title="test task", status="pending")
    assert t.stem == "0001_test"
    assert t.priority == "medium"


def test_goal_summary_basic():
    g = GoalSummary(id=1, text="goal", status="active", priority="high")
    assert g.id == 1
    assert g.status == "active"


# =====================================================================
# ContextAssembler tests
# =====================================================================


@pytest.mark.asyncio
async def test_assembler_empty_dirs(tmp_path):
    assembler = ContextAssembler(base_path=str(tmp_path))
    report = make_report()
    ctx = await assembler.assemble(report)
    assert ctx.progress_report == report
    assert ctx.pending_tasks == []
    assert ctx.active_goals == []
    assert ctx.recent_decisions == []
    assert ctx.time_of_day in ("morning", "afternoon", "evening", "night")


@pytest.mark.asyncio
async def test_assembler_loads_pending_tasks(tmp_path):
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    (tasks_dir / "0001_test_task.md").write_text("# Test\n")
    (tasks_dir / "0002_done_task.md.done").write_text("# Done\n")

    assembler = ContextAssembler(base_path=str(tmp_path))
    ctx = await assembler.assemble(make_report())
    assert len(ctx.pending_tasks) == 1
    assert ctx.pending_tasks[0].stem == "0001_test_task"


@pytest.mark.asyncio
async def test_assembler_loads_goals(tmp_path):
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    goals = {"goals": [{"id": 1, "text": "goal1", "status": "active", "priority": "high"}], "next_id": 2}
    (state_dir / "goals.json").write_text(json.dumps(goals))

    assembler = ContextAssembler(base_path=str(tmp_path))
    ctx = await assembler.assemble(make_report())
    assert len(ctx.active_goals) == 1
    assert ctx.active_goals[0].text == "goal1"


@pytest.mark.asyncio
async def test_assembler_skips_inactive_goals(tmp_path):
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    goals = {
        "goals": [
            {"id": 1, "text": "done", "status": "completed", "priority": "high"},
            {"id": 2, "text": "active", "status": "active", "priority": "normal"},
        ]
    }
    (state_dir / "goals.json").write_text(json.dumps(goals))

    assembler = ContextAssembler(base_path=str(tmp_path))
    ctx = await assembler.assemble(make_report())
    assert len(ctx.active_goals) == 1
    assert ctx.active_goals[0].id == 2


@pytest.mark.asyncio
async def test_assembler_loads_decision_history(tmp_path):
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    decisions = [{"mode": "monitor", "reason": "all good"} for _ in range(10)]
    (state_dir / "decision_history.json").write_text(json.dumps(decisions))

    assembler = ContextAssembler(base_path=str(tmp_path))
    ctx = await assembler.assemble(make_report())
    assert len(ctx.recent_decisions) == 10  # limited to 20, only 10 exist


@pytest.mark.asyncio
async def test_assembler_handles_corrupt_goals(tmp_path):
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    (state_dir / "goals.json").write_text("not json")

    assembler = ContextAssembler(base_path=str(tmp_path))
    ctx = await assembler.assemble(make_report())
    assert ctx.active_goals == []


def test_classify_time():
    # Smoke test -- just ensure it returns a valid bucket
    result = ContextAssembler._classify_time()
    assert result in ("morning", "afternoon", "evening", "night")


# =====================================================================
# ActionMode enum tests
# =====================================================================


def test_action_mode_values():
    assert ActionMode.RESEARCH == "research"
    assert ActionMode.PLAN == "plan"
    assert ActionMode.EXECUTE == "execute"
    assert ActionMode.MONITOR == "monitor"
    assert ActionMode.VALIDATE == "validate"
    assert ActionMode.REPAIR == "repair"
    assert ActionMode.ESCALATE == "escalate"
    assert len(ActionMode) == 7


# =====================================================================
# DecisionConfig tests
# =====================================================================


def test_decision_config_defaults():
    cfg = DecisionConfig()
    assert cfg.research_threshold == 40.0
    assert cfg.plan_threshold == 60.0
    assert cfg.cooldown_minutes == 30


def test_decision_config_custom():
    cfg = DecisionConfig(research_threshold=30.0, max_research_per_day=5)
    assert cfg.research_threshold == 30.0
    assert cfg.max_research_per_day == 5


# =====================================================================
# Decision model tests
# =====================================================================


def test_decision_model():
    d = Decision(mode=ActionMode.MONITOR, reason="all good")
    assert d.mode == ActionMode.MONITOR
    assert d.reason == "all good"
    assert d.confidence == "medium"
    assert d.decided_at is not None


def test_decision_serialization():
    d = Decision(mode=ActionMode.REPAIR, reason="broke", target_dimension="risk_engine")
    data = json.loads(d.model_dump_json())
    assert data["mode"] == "repair"
    assert data["target_dimension"] == "risk_engine"


# =====================================================================
# DecisionEngine -- mode classification tests
# =====================================================================


@pytest.mark.asyncio
async def test_engine_research_mode(tmp_path):
    """Knowledge gaps + low score -> RESEARCH."""
    report = make_report(
        dims={"system_health": 30.0, "execution_pipeline": 80.0},
        sub_metrics_map={"system_health": [SubMetric(name="m1", value=0.0), SubMetric(name="m2", value=0.0)]},
        warnings_map={"system_health": ["warn1", "warn2"]},
    )
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.RESEARCH
    assert "system_health" in decision.reason


@pytest.mark.asyncio
async def test_engine_plan_mode(tmp_path):
    """Low score + no existing plan -> PLAN."""
    report = make_report(dims={"system_health": 50.0, "execution_pipeline": 80.0})
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.PLAN
    assert decision.target_dimension == "system_health"


@pytest.mark.asyncio
async def test_engine_execute_mode(tmp_path):
    """Low score + existing plan -> EXECUTE."""
    report = make_report(dims={"system_health": 50.0, "execution_pipeline": 80.0})
    recent = [{"mode": "plan", "target_dimension": "system_health"}]
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report, recent_decisions=recent))
    assert decision.mode == ActionMode.EXECUTE


@pytest.mark.asyncio
async def test_engine_repair_mode(tmp_path):
    """Regression detected -> REPAIR."""
    report = make_report(
        dims={"system_health": 50.0, "execution_pipeline": 80.0},
        trends={"system_health": (65.0, "degrading"), "execution_pipeline": (80.0, "stable")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0), base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.REPAIR
    assert decision.target_dimension == "system_health"


@pytest.mark.asyncio
async def test_engine_validate_mode(tmp_path):
    """All above target + recent improvements -> VALIDATE."""
    report = make_report(
        dims={"system_health": 85.0, "execution_pipeline": 90.0},
        trends={"system_health": (80.0, "improving"), "execution_pipeline": (85.0, "stable")},
    )
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.VALIDATE


@pytest.mark.asyncio
async def test_engine_monitor_mode(tmp_path):
    """All fine, no changes -> MONITOR."""
    report = make_report(
        dims={"system_health": 85.0, "execution_pipeline": 90.0},
        trends={"system_health": (85.0, "stable"), "execution_pipeline": (90.0, "stable")},
    )
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.MONITOR


@pytest.mark.asyncio
async def test_engine_empty_dimensions(tmp_path):
    """No dimensions -> MONITOR."""
    report = ProgressReport(overall_score=0.0, dimensions={}, trends={})
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.MONITOR


@pytest.mark.asyncio
async def test_engine_all_zero(tmp_path):
    """All dimensions at 0 -> RESEARCH (not panic)."""
    report = make_report(
        dims={"system_health": 0.0, "execution_pipeline": 0.0},
        sub_metrics_map={
            "system_health": [SubMetric(name="m1", value=0.0)],
            "execution_pipeline": [SubMetric(name="m1", value=0.0)],
        },
        warnings_map={"system_health": ["err1", "err2"], "execution_pipeline": ["err1", "err2"]},
    )
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.mode == ActionMode.RESEARCH


@pytest.mark.asyncio
async def test_engine_decision_has_reason(tmp_path):
    """Every decision must have a reason (No Blind Execution rule)."""
    report = make_report()
    engine = DecisionEngine(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(exist_ok=True)
    decision = await engine.decide(make_context(report))
    assert decision.reason != ""
    assert len(decision.reason) > 10


@pytest.mark.asyncio
async def test_engine_persists_decision(tmp_path):
    """Decision is persisted to STATE/decision_history.json."""
    (tmp_path / "STATE").mkdir()
    engine = DecisionEngine(base_path=str(tmp_path))
    await engine.decide(make_context())
    history_path = tmp_path / "STATE" / "decision_history.json"
    assert history_path.exists()
    data = json.loads(history_path.read_text())
    assert len(data) == 1
    assert "mode" in data[0]


@pytest.mark.asyncio
async def test_engine_appends_decisions(tmp_path):
    """Multiple identical decisions are deduped (v87 P3); different decisions append."""
    (tmp_path / "STATE").mkdir()
    engine = DecisionEngine(base_path=str(tmp_path))
    await engine.decide(make_context())
    await engine.decide(make_context())
    data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
    # Identical decisions are deduped into one entry with dedup_count
    assert len(data) == 1
    assert data[0].get("dedup_count", 1) == 2


@pytest.mark.asyncio
async def test_engine_history_limit(tmp_path):
    """History is capped at 100 entries."""
    (tmp_path / "STATE").mkdir()
    # Pre-populate with 99 entries
    existing = [{"mode": "monitor", "reason": f"entry_{i}"} for i in range(99)]
    (tmp_path / "STATE" / "decision_history.json").write_text(json.dumps(existing))
    engine = DecisionEngine(base_path=str(tmp_path))
    await engine.decide(make_context())
    await engine.decide(make_context())
    data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
    assert len(data) == 100  # capped


# =====================================================================
# Knowledge gap detection tests
# =====================================================================


def test_detect_knowledge_gaps_no_gaps():
    report = make_report()
    engine = DecisionEngine()
    gaps = engine._detect_knowledge_gaps(report)
    assert gaps == []


def test_detect_knowledge_gaps_many_warnings():
    report = make_report(warnings_map={"system_health": ["w1", "w2", "w3"]})
    engine = DecisionEngine()
    gaps = engine._detect_knowledge_gaps(report)
    assert "system_health" in gaps


def test_detect_knowledge_gaps_zero_metrics():
    report = make_report(
        sub_metrics_map={"system_health": [SubMetric(name="a", value=0.0), SubMetric(name="b", value=0.0)]}
    )
    engine = DecisionEngine()
    gaps = engine._detect_knowledge_gaps(report)
    assert "system_health" in gaps


# =====================================================================
# Regression detection tests
# =====================================================================


def test_detect_regression_none():
    report = make_report()
    engine = DecisionEngine()
    result = engine._detect_regression(report, [])
    assert result is None


def test_detect_regression_found():
    report = make_report(
        dims={"system_health": 50.0},
        trends={"system_health": (65.0, "degrading")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0))
    result = engine._detect_regression(report, [])
    assert result == "system_health"


def test_detect_regression_below_delta():
    report = make_report(
        dims={"system_health": 72.0},
        trends={"system_health": (78.0, "degrading")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0))
    result = engine._detect_regression(report, [])
    assert result is None


# =====================================================================
# TaskSpecGenerator tests
# =====================================================================


def test_task_spec_model():
    s = TaskSpec(title="test", body="body")
    assert s.priority == "medium"
    assert s.estimated_effort == "medium"


def test_generator_from_research_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.RESEARCH, reason="gaps", target_dimension="system_health")
    spec = gen.from_decision(d)
    assert spec.category == "research"
    assert "system health" in spec.title


def test_generator_from_plan_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.PLAN, reason="low", target_dimension="execution_pipeline")
    spec = gen.from_decision(d)
    assert spec.category == "plan"
    assert spec.priority == "high"


def test_generator_from_execute_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.EXECUTE, reason="go", target_dimension="risk_engine")
    spec = gen.from_decision(d)
    assert spec.category == "execute"


def test_generator_from_repair_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.REPAIR, reason="broke", target_dimension="strategy_validity")
    spec = gen.from_decision(d)
    assert spec.category == "repair"
    assert spec.priority == "high"


def test_generator_from_validate_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.VALIDATE, reason="check")
    spec = gen.from_decision(d)
    assert spec.category == "validate"


def test_generator_from_monitor_decision():
    gen = TaskSpecGenerator()
    d = Decision(mode=ActionMode.MONITOR, reason="all good")
    spec = gen.from_decision(d)
    assert spec.category == "monitor"
    assert spec.priority == "low"


def test_write_task_file(tmp_path):
    gen = TaskSpecGenerator(base_path=str(tmp_path))
    spec = TaskSpec(
        title="Test Task",
        body="Do the thing.",
        priority="high",
        category="execute",
        target_dimension="system_health",
        goal_justification="Because score is low",
    )
    path = gen.write_task_file(spec)
    assert path.exists()
    content = path.read_text()
    assert "priority: high" in content
    assert "category: execute" in content
    assert "# Test Task" in content
    assert "Do the thing." in content


def test_write_task_sequence(tmp_path):
    gen = TaskSpecGenerator(base_path=str(tmp_path))
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    (tasks_dir / "0555_existing.md").write_text("# Existing\n")
    spec = TaskSpec(title="New Task", body="body")
    path = gen.write_task_file(spec)
    assert "0556_" in path.name


def test_slugify():
    assert TaskSpecGenerator._slugify("Hello World!") == "hello_world"
    assert TaskSpecGenerator._slugify("Plan execution_pipeline improvement") == "plan_execution_pipeline_improvement"


def test_slugify_max_length():
    long_title = "a" * 100
    slug = TaskSpecGenerator._slugify(long_title, max_len=20)
    assert len(slug) == 20


def test_write_task_creates_dir(tmp_path):
    gen = TaskSpecGenerator(base_path=str(tmp_path))
    # TASKS dir doesn't exist yet
    spec = TaskSpec(title="Auto Dir", body="body")
    path = gen.write_task_file(spec)
    assert path.exists()
    assert (tmp_path / "TASKS").is_dir()


# =====================================================================
# has_existing_plan tests
# =====================================================================


def test_has_plan_from_decisions():
    engine = DecisionEngine()
    ctx = make_context(recent_decisions=[{"mode": "plan", "target_dimension": "risk_engine"}])
    assert engine._has_existing_plan("risk_engine", ctx) is True
    assert engine._has_existing_plan("system_health", ctx) is False


def test_has_plan_from_task_titles():
    engine = DecisionEngine()
    tasks = [TaskSummary(stem="0001_plan_system_health", title="plan system health improvement", status="pending")]
    ctx = make_context(pending_tasks=tasks)
    assert engine._has_existing_plan("system_health", ctx) is True


def test_has_plan_no_match():
    engine = DecisionEngine()
    ctx = make_context()
    assert engine._has_existing_plan("system_health", ctx) is False


def test_has_plan_none_dimension():
    engine = DecisionEngine()
    assert engine._has_existing_plan(None, make_context()) is False


# =====================================================================
# Regression GREEN-score guard tests (Phase 3 false-alarm fix)
# =====================================================================


def test_regression_suppressed_when_green():
    """Score 83 >= target 70 => regression suppressed even though delta 12 > 10."""
    report = make_report(
        dims={"system_health": 83.0},
        trends={"system_health": (95.0, "degrading")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0))
    result = engine._detect_regression(report, [])
    assert result is None


def test_regression_fires_when_below_target():
    """Score 55 < target 70 and delta 15 > 10 => regression fires."""
    report = make_report(
        dims={"system_health": 55.0},
        trends={"system_health": (70.0, "degrading")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0))
    result = engine._detect_regression(report, [])
    assert result == "system_health"


def test_regression_fires_at_boundary():
    """Score 69.9 < target 70 and delta 12.1 > 10 => regression fires."""
    report = make_report(
        dims={"system_health": 69.9},
        trends={"system_health": (82.0, "degrading")},
    )
    engine = DecisionEngine(config=DecisionConfig(regression_delta=10.0))
    result = engine._detect_regression(report, [])
    assert result == "system_health"


# =====================================================================
# Phase 2 false-alarm fix: REPAIR/EXECUTE rate-limiting tests
# =====================================================================


def test_repair_daily_cap_blocks_fifth_repair():
    """4 repair decisions today should hit the daily cap (max_repair_per_day=4)."""
    from datetime import datetime, timedelta, timezone

    # Use zero cooldown so we isolate the daily-cap logic from the
    # cooldown logic — avoids false positives near midnight UTC.
    engine = DecisionEngine(config=DecisionConfig(cooldown_minutes_repair=0))
    now = datetime.now(timezone.utc)
    # Pin decisions to today at fixed times so they always fall within
    # the current UTC day regardless of when the test runs.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    recent = [
        {
            "mode": "repair",
            "target_dimension": "system_health",
            "decided_at": (day_start + timedelta(minutes=1 + i)).isoformat(),
        }
        for i in range(4)
    ]
    ctx = make_context(recent_decisions=recent)
    # The 5th repair should be blocked by daily limit
    assert engine._check_cooldown(ActionMode.REPAIR, ctx) is True


def test_repair_cooldown_120_minutes():
    """REPAIR has a 120-minute cooldown. A repair 90 min ago blocks; 121 min ago does not."""
    from datetime import datetime, timedelta, timezone

    engine = DecisionEngine()
    now = datetime.now(timezone.utc)

    # 90 minutes ago -> should be blocked (within 120-min cooldown)
    recent_90 = [
        {
            "mode": "repair",
            "target_dimension": "system_health",
            "decided_at": (now - timedelta(minutes=90)).isoformat(),
        }
    ]
    ctx_90 = make_context(recent_decisions=recent_90)
    assert engine._check_cooldown(ActionMode.REPAIR, ctx_90) is True

    # 121 minutes ago -> should NOT be blocked (outside 120-min cooldown)
    recent_121 = [
        {
            "mode": "repair",
            "target_dimension": "system_health",
            "decided_at": (now - timedelta(minutes=121)).isoformat(),
        }
    ]
    ctx_121 = make_context(recent_decisions=recent_121)
    assert engine._check_cooldown(ActionMode.REPAIR, ctx_121) is False


def test_non_repair_uses_30min_cooldown():
    """Non-REPAIR modes (e.g. RESEARCH) still use the default 30-min cooldown."""
    from datetime import datetime, timedelta, timezone

    engine = DecisionEngine()
    now = datetime.now(timezone.utc)

    # 31 minutes ago -> should NOT be blocked (outside 30-min default cooldown)
    recent = [
        {
            "mode": "research",
            "target_dimension": "system_health",
            "decided_at": (now - timedelta(minutes=31)).isoformat(),
        }
    ]
    ctx = make_context(recent_decisions=recent)
    assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is False
