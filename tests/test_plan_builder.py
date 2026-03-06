"""Tests for planner.plan_builder — intent parsing and plan generation."""

from pathlib import Path

import pytest

from planner.plan_builder import PlanBuilder
from planner.schemas import SkillScore, TaskIntent
from planner.skill_history import SkillHistoryStore
from planner.skill_scorer import SkillScorer, DEFAULT_SKILLS_CATALOG


@pytest.fixture
def builder() -> PlanBuilder:
    return PlanBuilder()


def _intent(text: str, task_id: str = "0042") -> TaskIntent:
    return TaskIntent(task_id=task_id, goal=text, source="test")


def _ranked_skill(name: str, score: float = 0.5) -> SkillScore:
    return SkillScore(
        skill_name=name,
        semantic_match=score * 0.4,
        activation_rules=score * 0.2,
        recency=0.1,
        success_rate=0.1,
        total_score=score,
        reasons=[],
    )


# -- single-skill plan --------------------------------------------------------

def test_single_skill_plan(builder: PlanBuilder, tmp_path: Path):
    intent = _intent("run the task and dispatch workflow")
    store = SkillHistoryStore(path=tmp_path / "h.json")
    scorer = SkillScorer()
    ranked = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, store)
    plan = builder.build_plan(intent, ranked)
    assert plan.strategy == "single_skill"
    assert len(plan.steps) == 1
    assert plan.steps[0].step_id == "s1"


def test_single_skill_from_ranked(builder: PlanBuilder):
    intent = _intent("some custom query about file ops")
    ranked = [
        _ranked_skill("file_ops", 0.8),
        _ranked_skill("shell_ops", 0.3),
    ]
    plan = builder.build_plan(intent, ranked)
    assert plan.strategy == "single_skill"
    assert plan.steps[0].skill_name == "file_ops"


# -- multi-skill chain -------------------------------------------------------

def test_multi_skill_diagnose_chain(builder: PlanBuilder):
    intent = _intent("diagnose the failure in the logs")
    plan = builder.build_plan(intent, [])
    assert plan.strategy == "multi_skill"
    assert len(plan.steps) == 3
    assert plan.steps[0].skill_name == "log_triage"
    assert plan.steps[1].skill_name == "code_improve"
    assert plan.steps[2].skill_name == "system_supervisor"


def test_multi_skill_fix_chain(builder: PlanBuilder):
    intent = _intent("fix the broken parser code")
    plan = builder.build_plan(intent, [])
    assert plan.strategy == "multi_skill"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_name == "code_improve"
    assert plan.steps[1].skill_name == "system_supervisor"


def test_multi_skill_service_chain(builder: PlanBuilder):
    intent = _intent("restart the systemctl daemon")
    plan = builder.build_plan(intent, [])
    assert plan.strategy == "multi_skill"
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_name == "service_ops"
    assert plan.steps[1].skill_name == "system_supervisor"


# -- fallback to top-ranked skill ---------------------------------------------

def test_fallback_to_top_ranked(builder: PlanBuilder, tmp_path: Path):
    intent = _intent("search for information about Python web frameworks")
    store = SkillHistoryStore(path=tmp_path / "h.json")
    scorer = SkillScorer()
    ranked = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, store)
    plan = builder.build_plan(intent, ranked)
    assert plan.strategy == "single_skill"
    assert len(plan.steps) >= 1


def test_fallback_empty_plan_no_match(builder: PlanBuilder):
    intent = _intent("hello world")
    plan = builder.build_plan(intent, [])
    assert plan.steps == []


# -- deterministic plan IDs ---------------------------------------------------

def test_plan_id_derived_from_task_id(builder: PlanBuilder):
    intent = _intent("fix the bug", task_id="0099")
    plan = builder.build_plan(intent, [])
    assert plan.plan_id == "plan_0099"


def test_plan_id_stable(builder: PlanBuilder):
    intent = _intent("restart the service", task_id="0050")
    plan1 = builder.build_plan(intent, [])
    plan2 = builder.build_plan(intent, [])
    assert plan1.plan_id == plan2.plan_id


# -- success criteria included ------------------------------------------------

def test_multi_skill_success_criteria(builder: PlanBuilder):
    intent = _intent("diagnose the crash")
    plan = builder.build_plan(intent, [])
    assert "contract valid" in plan.success_criteria
    assert "verification present" in plan.success_criteria


def test_single_skill_success_criteria(builder: PlanBuilder):
    intent = _intent("search for documentation")
    ranked = [_ranked_skill("web_research", 0.5)]
    plan = builder.build_plan(intent, ranked)
    assert "contract valid" in plan.success_criteria


# -- build_intent exact fields ------------------------------------------------

def test_build_intent_sets_goal(builder: PlanBuilder):
    intent = builder.build_intent("t001", "diagnose the error and fix the code")
    assert intent.goal == "diagnose the error and fix the code"
    assert intent.task_id == "t001"
    assert intent.source == "worker"


def test_build_intent_default_fields(builder: PlanBuilder):
    intent = builder.build_intent("t001", "some task")
    assert intent.priority == "normal"
    assert intent.constraints == []
    assert intent.context == {}


# -- status defaults ----------------------------------------------------------

def test_plan_initial_status(builder: PlanBuilder):
    intent = _intent("diagnose the crash")
    plan = builder.build_plan(intent, [])
    assert plan.status == "queued"


def test_step_initial_status(builder: PlanBuilder):
    intent = _intent("fix the bug")
    plan = builder.build_plan(intent, [])
    for step in plan.steps:
        assert step.status == "queued"


# -- step inputs field --------------------------------------------------------

def test_step_has_inputs_field(builder: PlanBuilder):
    intent = _intent("fix the bug")
    plan = builder.build_plan(intent, [])
    for step in plan.steps:
        assert hasattr(step, "inputs")
        assert isinstance(step.inputs, dict)
