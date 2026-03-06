"""Tests for planner.skill_scorer — deterministic 4-component scoring."""

from pathlib import Path

import pytest

from planner.schemas import SkillScore, TaskIntent
from planner.skill_history import SkillHistoryStore
from planner.skill_scorer import SkillScorer, DEFAULT_SKILLS_CATALOG


@pytest.fixture
def tmp_store(tmp_path: Path) -> SkillHistoryStore:
    return SkillHistoryStore(path=tmp_path / "history.json")


@pytest.fixture
def scorer() -> SkillScorer:
    return SkillScorer()


def _intent(text: str, task_id: str = "t001") -> TaskIntent:
    return TaskIntent(task_id=task_id, goal=text, source="test")


# -- semantic match ranking ---------------------------------------------------

def test_semantic_match_log_triage(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("diagnose the crash from logs and traceback"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    names = [s.skill_name for s in scores]
    assert "log_triage" in names
    lt = next(s for s in scores if s.skill_name == "log_triage")
    assert lt.semantic_match > 0


def test_semantic_match_code_improve(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("fix the bug and refactor the code"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    names = [s.skill_name for s in scores]
    assert "code_improve" in names


def test_semantic_match_no_match(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("hello world xyz abc"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    assert scores == []


def test_semantic_match_multiple_skills(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("restart the service, diagnose the error"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    names = [s.skill_name for s in scores]
    assert "service_ops" in names
    assert "log_triage" in names


# -- activation rule bonus ----------------------------------------------------

def test_activation_keywords_boost(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("restart the systemctl daemon"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    service_score = next(s for s in scores if s.skill_name == "service_ops")
    assert service_score.activation_rules > 0
    # activation_rules adds to total beyond semantic_match alone
    assert service_score.total_score > service_score.semantic_match


# -- success-rate weighting ---------------------------------------------------

def test_success_rate_boosts_score(tmp_path: Path):
    store = SkillHistoryStore(path=tmp_path / "h.json")
    for i in range(10):
        store.record_run("log_triage", success=True, duration_ms=100, retries=0)

    scorer = SkillScorer()
    scores = scorer.rank_skills(
        _intent("diagnose the error in logs"),
        DEFAULT_SKILLS_CATALOG,
        store,
    )
    lt = next(s for s in scores if s.skill_name == "log_triage")
    # success_rate = 1.0 → component = 1.0 * 0.2 = 0.2
    assert lt.success_rate > 0.1  # above neutral (0.5 * 0.2 = 0.1)


def test_poor_success_rate_penalizes(tmp_path: Path):
    store_bad = SkillHistoryStore(path=tmp_path / "bad.json")
    for i in range(10):
        store_bad.record_run("code_improve", success=False, duration_ms=100, retries=0)

    store_neutral = SkillHistoryStore(path=tmp_path / "neutral.json")

    scorer = SkillScorer()
    intent = _intent("fix the bug")
    bad_scores = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, store_bad)
    neutral_scores = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, store_neutral)

    bad_ci = next(s for s in bad_scores if s.skill_name == "code_improve")
    neutral_ci = next(s for s in neutral_scores if s.skill_name == "code_improve")
    # 0% success rate → 0.0 * 0.2 = 0.0 < 0.5 * 0.2 = 0.1
    assert bad_ci.success_rate < neutral_ci.success_rate


# -- recency weighting --------------------------------------------------------

def test_recency_weighting(tmp_path: Path):
    store = SkillHistoryStore(path=tmp_path / "h.json")
    store.record_run("log_triage", success=True, duration_ms=100, retries=0)

    scorer = SkillScorer()
    scores = scorer.rank_skills(
        _intent("diagnose error"),
        DEFAULT_SKILLS_CATALOG,
        store,
    )
    lt = next(s for s in scores if s.skill_name == "log_triage")
    # Recently used → recency_raw close to 1.0 → component close to 0.2
    assert lt.recency > 0.15


# -- stable sort behavior ----------------------------------------------------

def test_stable_sort_deterministic(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    intent = _intent("diagnose the crash from logs and traceback")
    scores1 = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, tmp_store)
    scores2 = scorer.rank_skills(intent, DEFAULT_SKILLS_CATALOG, tmp_store)
    assert [s.skill_name for s in scores1] == [s.skill_name for s in scores2]
    assert [s.total_score for s in scores1] == [s.total_score for s in scores2]


def test_descending_score_order(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("diagnose error fix code restart service"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    for i in range(len(scores) - 1):
        assert scores[i].total_score >= scores[i + 1].total_score


# -- unknown skill history fallback -------------------------------------------

def test_unknown_history_uses_neutral_scores(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    scores = scorer.rank_skills(
        _intent("fix the bug"),
        DEFAULT_SKILLS_CATALOG,
        tmp_store,
    )
    ci = next(s for s in scores if s.skill_name == "code_improve")
    # Unknown history → recency=0.5*0.2=0.1, success_rate=0.5*0.2=0.1
    assert ci.recency == pytest.approx(0.1, abs=0.001)
    assert ci.success_rate == pytest.approx(0.1, abs=0.001)


# -- score_skill returns exact SkillScore shape --------------------------------

def test_score_skill_has_all_components(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    intent = _intent("diagnose the error in logs")
    meta = DEFAULT_SKILLS_CATALOG[0]  # log_triage
    score = scorer.score_skill(intent, meta, tmp_store)
    assert isinstance(score, SkillScore)
    assert hasattr(score, "skill_name")
    assert hasattr(score, "semantic_match")
    assert hasattr(score, "activation_rules")
    assert hasattr(score, "recency")
    assert hasattr(score, "success_rate")
    assert hasattr(score, "total_score")
    assert hasattr(score, "reasons")


def test_score_skill_total_equals_sum(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    intent = _intent("diagnose the error in logs")
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(intent, meta, tmp_store)
    expected = (
        score.semantic_match
        + score.activation_rules
        + score.recency
        + score.success_rate
    )
    assert score.total_score == pytest.approx(expected, abs=0.001)


def test_score_skill_reasons_is_list(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    intent = _intent("diagnose the error from logs")
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(intent, meta, tmp_store)
    assert isinstance(score.reasons, list)


# -- component ranges --------------------------------------------------------

def test_semantic_match_capped_at_04(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    intent = _intent("diagnose triage errors log files tracebacks crash exception debug investigate")
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(intent, meta, tmp_store)
    assert score.semantic_match <= 0.4


def test_activation_rules_capped_at_02(scorer: SkillScorer, tmp_store: SkillHistoryStore):
    # Use all keywords for log_triage
    all_kws = " ".join(DEFAULT_SKILLS_CATALOG[0]["keywords"])
    intent = _intent(all_kws)
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(intent, meta, tmp_store)
    assert score.activation_rules <= 0.2


def test_recency_capped_at_02(tmp_path: Path):
    store = SkillHistoryStore(path=tmp_path / "h.json")
    store.record_run("log_triage", success=True, duration_ms=100, retries=0)
    scorer = SkillScorer()
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(_intent("diagnose error"), meta, store)
    assert score.recency <= 0.2


def test_success_rate_capped_at_02(tmp_path: Path):
    store = SkillHistoryStore(path=tmp_path / "h.json")
    for i in range(100):
        store.record_run("log_triage", success=True, duration_ms=100, retries=0)
    scorer = SkillScorer()
    meta = DEFAULT_SKILLS_CATALOG[0]
    score = scorer.score_skill(_intent("diagnose error"), meta, store)
    assert score.success_rate <= 0.2
