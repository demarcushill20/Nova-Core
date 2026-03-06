"""Tests for planner.improvement_planner — bounded self-improvement loop."""

import json
from pathlib import Path

import pytest

from planner.improvement_planner import (
    CAT_CONTRACT_FAILURE,
    CAT_LOW_GRADE,
    CAT_RETRY_PATTERN,
    CAT_SLOW_EXECUTION,
    CAT_VERIFICATION_WEAKNESS,
    ImprovementPlanner,
)
from planner.schemas import (
    VALID_SEVERITIES,
    ExecutionEvaluation,
    HealthFinding,
    ImprovementPlan,
    ImprovementResult,
    PlanEvaluation,
)


@pytest.fixture
def planner() -> ImprovementPlanner:
    return ImprovementPlanner()


@pytest.fixture
def tmp_improvement_dir(tmp_path: Path, monkeypatch):
    """Redirect IMPROVEMENT_DIR to a temp directory."""
    imp_dir = tmp_path / "improvement_runs"
    monkeypatch.setattr(
        "planner.improvement_planner.IMPROVEMENT_DIR", imp_dir
    )
    return imp_dir


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _step_eval(
    step_id: str = "s1",
    execution_success: bool = True,
    contract_valid: bool = True,
    retry_penalty: float = 0.0,
    duration_score: float = 0.15,
    verification_score: float = 0.20,
    total_score: float = 1.0,
    grade: str = "A",
) -> ExecutionEvaluation:
    return ExecutionEvaluation(
        step_id=step_id,
        execution_success=execution_success,
        contract_valid=contract_valid,
        retry_penalty=retry_penalty,
        duration_score=duration_score,
        verification_score=verification_score,
        total_score=total_score,
        grade=grade,
    )


def _plan_eval(
    plan_id: str = "plan_t001",
    step_evaluations: list[ExecutionEvaluation] | None = None,
    aggregate_score: float = 1.0,
    grade: str = "A",
    followup_recommended: bool = False,
    followup_reason: str | None = None,
) -> PlanEvaluation:
    return PlanEvaluation(
        plan_id=plan_id,
        step_evaluations=step_evaluations or [],
        aggregate_score=aggregate_score,
        grade=grade,
        summary=f"test summary grade={grade}",
        followup_recommended=followup_recommended,
        followup_reason=followup_reason,
    )


# =============================================================================
# build_health_findings
# =============================================================================


class TestBuildHealthFindings:
    """Tests for ImprovementPlanner.build_health_findings."""

    def test_perfect_plan_no_findings(self, planner: ImprovementPlanner):
        """Grade A plan with all perfect steps → no findings."""
        pe = _plan_eval(
            grade="A",
            aggregate_score=0.95,
            step_evaluations=[_step_eval()],
        )
        findings = planner.build_health_findings(pe)
        assert findings == []

    def test_low_grade_finding(self, planner: ImprovementPlanner):
        """Grade D plan → low_grade_execution finding."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.45,
            step_evaluations=[
                _step_eval(grade="D", total_score=0.45),
            ],
        )
        findings = planner.build_health_findings(pe)
        cats = [f.category for f in findings]
        assert CAT_LOW_GRADE in cats
        lg = [f for f in findings if f.category == CAT_LOW_GRADE][0]
        assert lg.severity == "high"
        assert "D" in lg.summary
        assert len(lg.evidence) > 0

    def test_grade_F_critical_severity(self, planner: ImprovementPlanner):
        """Grade F → critical severity finding."""
        pe = _plan_eval(grade="F", aggregate_score=0.15)
        findings = planner.build_health_findings(pe)
        lg = [f for f in findings if f.category == CAT_LOW_GRADE]
        assert len(lg) == 1
        assert lg[0].severity == "critical"

    def test_contract_failure_finding(self, planner: ImprovementPlanner):
        """Steps with invalid contracts → contract_failure finding."""
        pe = _plan_eval(
            grade="C",
            aggregate_score=0.65,
            step_evaluations=[
                _step_eval(step_id="s1", contract_valid=False),
                _step_eval(step_id="s2", contract_valid=True),
            ],
        )
        findings = planner.build_health_findings(pe)
        cf = [f for f in findings if f.category == CAT_CONTRACT_FAILURE]
        assert len(cf) == 1
        assert "s1" in cf[0].summary
        assert cf[0].severity == "medium"

    def test_multiple_contract_failures_high_severity(
        self, planner: ImprovementPlanner
    ):
        """Multiple contract failures → high severity."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.42,
            step_evaluations=[
                _step_eval(step_id="s1", contract_valid=False),
                _step_eval(step_id="s2", contract_valid=False),
            ],
        )
        findings = planner.build_health_findings(pe)
        cf = [f for f in findings if f.category == CAT_CONTRACT_FAILURE]
        assert len(cf) == 1
        assert cf[0].severity == "high"

    def test_retry_pattern_finding(self, planner: ImprovementPlanner):
        """Steps with retry penalty → retry_pattern finding."""
        pe = _plan_eval(
            grade="B",
            aggregate_score=0.80,
            step_evaluations=[
                _step_eval(step_id="s1", retry_penalty=0.10),
            ],
        )
        findings = planner.build_health_findings(pe)
        rp = [f for f in findings if f.category == CAT_RETRY_PATTERN]
        assert len(rp) == 1
        assert rp[0].severity == "medium"

    def test_slow_execution_finding(self, planner: ImprovementPlanner):
        """Steps with zero duration score → slow_execution finding."""
        pe = _plan_eval(
            grade="B",
            aggregate_score=0.80,
            step_evaluations=[
                _step_eval(
                    step_id="s1",
                    duration_score=0.0,
                    execution_success=True,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        se = [f for f in findings if f.category == CAT_SLOW_EXECUTION]
        assert len(se) == 1
        assert se[0].severity == "low"

    def test_slow_execution_skips_failed_steps(
        self, planner: ImprovementPlanner
    ):
        """Slow but failed steps are not flagged as slow_execution."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.45,
            step_evaluations=[
                _step_eval(
                    step_id="s1",
                    duration_score=0.0,
                    execution_success=False,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        se = [f for f in findings if f.category == CAT_SLOW_EXECUTION]
        assert len(se) == 0

    def test_verification_weakness_finding(
        self, planner: ImprovementPlanner
    ):
        """Steps with low verification score → verification_weakness."""
        pe = _plan_eval(
            grade="B",
            aggregate_score=0.80,
            step_evaluations=[
                _step_eval(
                    step_id="s1",
                    verification_score=0.05,
                    execution_success=True,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        vw = [f for f in findings if f.category == CAT_VERIFICATION_WEAKNESS]
        assert len(vw) == 1
        assert vw[0].severity == "medium"

    def test_verification_weakness_skips_failed(
        self, planner: ImprovementPlanner
    ):
        """Failed steps with low verification are not flagged."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.45,
            step_evaluations=[
                _step_eval(
                    step_id="s1",
                    verification_score=0.0,
                    execution_success=False,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        vw = [f for f in findings if f.category == CAT_VERIFICATION_WEAKNESS]
        assert len(vw) == 0

    def test_multiple_categories(self, planner: ImprovementPlanner):
        """A bad plan can produce multiple finding categories."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.42,
            step_evaluations=[
                _step_eval(
                    step_id="s1",
                    contract_valid=False,
                    retry_penalty=0.10,
                    verification_score=0.05,
                    execution_success=True,
                    duration_score=0.0,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        cats = {f.category for f in findings}
        assert CAT_LOW_GRADE in cats
        assert CAT_CONTRACT_FAILURE in cats
        assert CAT_RETRY_PATTERN in cats
        assert CAT_SLOW_EXECUTION in cats
        assert CAT_VERIFICATION_WEAKNESS in cats

    def test_finding_ids_unique(self, planner: ImprovementPlanner):
        """All finding IDs must be unique."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.42,
            step_evaluations=[
                _step_eval(
                    contract_valid=False,
                    retry_penalty=0.10,
                    verification_score=0.05,
                    execution_success=True,
                    duration_score=0.0,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        ids = [f.finding_id for f in findings]
        assert len(ids) == len(set(ids))

    def test_cross_plan_systemic_finding(self, planner: ImprovementPlanner):
        """Recent plan states with multiple D/F grades → systemic finding."""
        pe = _plan_eval(grade="D", aggregate_score=0.42)
        recent = [
            {
                "plan": {"plan_id": "plan_a"},
                "evaluation": {"grade": "D", "aggregate_score": 0.45},
            },
            {
                "plan": {"plan_id": "plan_b"},
                "evaluation": {"grade": "F", "aggregate_score": 0.15},
            },
        ]
        findings = planner.build_health_findings(pe, recent)
        systemic = [
            f for f in findings
            if f.category == CAT_LOW_GRADE and f.severity == "critical"
            and "systemic" in f.summary
        ]
        assert len(systemic) == 1

    def test_no_cross_plan_finding_with_one_low_grade(
        self, planner: ImprovementPlanner
    ):
        """Single D/F in recent plans → no systemic finding."""
        pe = _plan_eval(grade="D", aggregate_score=0.42)
        recent = [
            {
                "plan": {"plan_id": "plan_a"},
                "evaluation": {"grade": "D", "aggregate_score": 0.45},
            },
            {
                "plan": {"plan_id": "plan_b"},
                "evaluation": {"grade": "A", "aggregate_score": 0.95},
            },
        ]
        findings = planner.build_health_findings(pe, recent)
        systemic = [
            f for f in findings
            if "systemic" in f.summary
        ]
        assert len(systemic) == 0


# =============================================================================
# build_improvement_plan
# =============================================================================


class TestBuildImprovementPlan:
    """Tests for ImprovementPlanner.build_improvement_plan."""

    def test_no_findings_skipped(self, planner: ImprovementPlanner):
        """No findings → plan with status 'skipped', max_steps=0."""
        plan = planner.build_improvement_plan([])
        assert plan.status == "skipped"
        assert plan.max_steps == 0
        assert plan.max_files_changed == 0
        assert not plan.findings

    def test_findings_produce_queued_plan(self, planner: ImprovementPlanner):
        """Findings → plan with status 'queued' and bounded parameters."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_LOW_GRADE,
                severity="high",
                summary="Grade D",
                evidence=["aggregate_score=0.45"],
            )
        ]
        plan = planner.build_improvement_plan(findings, "plan_src")
        assert plan.status == "queued"
        assert plan.source_plan_id == "plan_src"
        assert plan.max_steps >= 1
        assert plan.max_steps <= 3
        assert plan.max_files_changed <= 5
        assert len(plan.goals) >= 1
        assert len(plan.allowed_skills) >= 1

    def test_critical_finding_requires_human_review(
        self, planner: ImprovementPlanner
    ):
        """Critical severity → requires_human_review=True."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_LOW_GRADE,
                severity="critical",
                summary="Systemic failure",
            )
        ]
        plan = planner.build_improvement_plan(findings)
        assert plan.requires_human_review is True

    def test_non_critical_no_human_review(
        self, planner: ImprovementPlanner
    ):
        """Medium severity only → requires_human_review=False."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_RETRY_PATTERN,
                severity="medium",
                summary="Retries happening",
            )
        ]
        plan = planner.build_improvement_plan(findings)
        assert plan.requires_human_review is False

    def test_max_steps_capped(self, planner: ImprovementPlanner):
        """Even with many findings, max_steps capped at 3."""
        findings = [
            HealthFinding(
                finding_id=f"hf_{i:03d}",
                category=CAT_CONTRACT_FAILURE,
                severity="medium",
                summary=f"Finding {i}",
            )
            for i in range(10)
        ]
        plan = planner.build_improvement_plan(findings)
        assert plan.max_steps <= 3

    def test_allowed_skills_present(self, planner: ImprovementPlanner):
        """Plan specifies allowed skills."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_LOW_GRADE,
                severity="high",
                summary="Low grade",
            )
        ]
        plan = planner.build_improvement_plan(findings)
        assert "code_improve" in plan.allowed_skills
        assert "repo_health_check" in plan.allowed_skills

    def test_goals_derived_from_categories(
        self, planner: ImprovementPlanner
    ):
        """Goals correspond to finding categories."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_CONTRACT_FAILURE,
                severity="medium",
                summary="Contract failure",
            ),
            HealthFinding(
                finding_id="hf_002",
                category=CAT_SLOW_EXECUTION,
                severity="low",
                summary="Slow step",
            ),
        ]
        plan = planner.build_improvement_plan(findings)
        assert len(plan.goals) == 2
        assert any("contract" in g.lower() for g in plan.goals)
        assert any("slow" in g.lower() or "30s" in g.lower() for g in plan.goals)

    def test_improvement_id_generated(self, planner: ImprovementPlanner):
        """Plan has a non-empty improvement_id."""
        findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_LOW_GRADE,
                severity="high",
                summary="Low grade",
            )
        ]
        plan = planner.build_improvement_plan(findings)
        assert plan.improvement_id.startswith("imp_")
        assert len(plan.improvement_id) > 4

    def test_high_severity_increases_max_files(
        self, planner: ImprovementPlanner
    ):
        """High-severity finding allows more files to be changed."""
        medium_findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_RETRY_PATTERN,
                severity="medium",
                summary="Retries",
            )
        ]
        high_findings = [
            HealthFinding(
                finding_id="hf_001",
                category=CAT_LOW_GRADE,
                severity="high",
                summary="Low grade",
            )
        ]
        medium_plan = planner.build_improvement_plan(medium_findings)
        high_plan = planner.build_improvement_plan(high_findings)
        assert high_plan.max_files_changed >= medium_plan.max_files_changed


# =============================================================================
# should_execute
# =============================================================================


class TestShouldExecute:
    """Tests for ImprovementPlanner.should_execute."""

    def test_queued_plan_with_findings_executes(
        self, planner: ImprovementPlanner
    ):
        """Queued plan with findings and max_steps > 0 → True."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[
                HealthFinding(
                    finding_id="hf_001",
                    category=CAT_LOW_GRADE,
                    severity="high",
                    summary="Low",
                )
            ],
            max_steps=2,
            status="queued",
        )
        assert planner.should_execute(plan) is True

    def test_no_findings_does_not_execute(
        self, planner: ImprovementPlanner
    ):
        """No findings → False."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[],
            max_steps=2,
            status="queued",
        )
        assert planner.should_execute(plan) is False

    def test_non_queued_does_not_execute(
        self, planner: ImprovementPlanner
    ):
        """Status not 'queued' → False."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[
                HealthFinding(
                    finding_id="hf_001",
                    category=CAT_LOW_GRADE,
                    severity="high",
                    summary="Low",
                )
            ],
            max_steps=2,
            status="running",
        )
        assert planner.should_execute(plan) is False

    def test_human_review_blocks_execution(
        self, planner: ImprovementPlanner
    ):
        """requires_human_review → False."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[
                HealthFinding(
                    finding_id="hf_001",
                    category=CAT_LOW_GRADE,
                    severity="critical",
                    summary="Critical",
                )
            ],
            max_steps=2,
            requires_human_review=True,
            status="queued",
        )
        assert planner.should_execute(plan) is False

    def test_zero_max_steps_does_not_execute(
        self, planner: ImprovementPlanner
    ):
        """max_steps=0 → False."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[
                HealthFinding(
                    finding_id="hf_001",
                    category=CAT_LOW_GRADE,
                    severity="high",
                    summary="Low",
                )
            ],
            max_steps=0,
            status="queued",
        )
        assert planner.should_execute(plan) is False

    def test_skipped_status_does_not_execute(
        self, planner: ImprovementPlanner
    ):
        """Status 'skipped' → False."""
        plan = ImprovementPlan(
            improvement_id="imp_1",
            findings=[],
            max_steps=0,
            status="skipped",
        )
        assert planner.should_execute(plan) is False


# =============================================================================
# persist_improvement_run
# =============================================================================


class TestPersistImprovementRun:
    """Tests for ImprovementPlanner.persist_improvement_run."""

    def test_persists_to_disk(
        self,
        planner: ImprovementPlanner,
        tmp_improvement_dir: Path,
    ):
        """Run is persisted as JSON."""
        plan = ImprovementPlan(
            improvement_id="imp_test_001",
            source_plan_id="plan_src",
            findings=[
                HealthFinding(
                    finding_id="hf_001",
                    category=CAT_LOW_GRADE,
                    severity="high",
                    summary="Low grade",
                    evidence=["score=0.45"],
                )
            ],
            goals=["Improve quality"],
            allowed_skills=["code_improve"],
            max_steps=2,
            max_files_changed=3,
            status="done",
        )
        result = ImprovementResult(
            improvement_id="imp_test_001",
            executed=True,
            final_status="done",
            evaluation_grade="B",
            evaluation_score=0.82,
            followup_recommended=False,
            notes=["Improvement applied"],
        )
        path = planner.persist_improvement_run(plan, result)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["improvement_id"] == "imp_test_001"
        assert data["source_plan_id"] == "plan_src"
        assert len(data["findings"]) == 1
        assert data["goals"] == ["Improve quality"]
        assert data["result"]["executed"] is True
        assert data["result"]["final_status"] == "done"
        assert data["result"]["evaluation_grade"] == "B"
        assert isinstance(data["saved_at"], float)

    def test_persist_creates_directory(
        self,
        planner: ImprovementPlanner,
        tmp_improvement_dir: Path,
    ):
        """Directory is created if it doesn't exist."""
        assert not tmp_improvement_dir.exists()
        plan = ImprovementPlan(
            improvement_id="imp_dir_test",
            status="done",
        )
        result = ImprovementResult(
            improvement_id="imp_dir_test",
            executed=False,
            final_status="skipped",
        )
        path = planner.persist_improvement_run(plan, result)
        assert path.exists()
        assert tmp_improvement_dir.exists()

    def test_persist_not_executed_run(
        self,
        planner: ImprovementPlanner,
        tmp_improvement_dir: Path,
    ):
        """Non-executed run is persisted with executed=False."""
        plan = ImprovementPlan(
            improvement_id="imp_skip",
            status="skipped",
        )
        result = ImprovementResult(
            improvement_id="imp_skip",
            executed=False,
            final_status="skipped",
            notes=["No findings"],
        )
        path = planner.persist_improvement_run(plan, result)
        data = json.loads(path.read_text())
        assert data["result"]["executed"] is False
        assert data["result"]["final_status"] == "skipped"


# =============================================================================
# HealthFinding dataclass
# =============================================================================


class TestHealthFindingDataclass:
    """Tests for HealthFinding schema correctness."""

    def test_required_fields(self):
        hf = HealthFinding(
            finding_id="hf_001",
            category="low_grade_execution",
            severity="high",
            summary="Test",
        )
        assert hf.finding_id == "hf_001"
        assert hf.category == "low_grade_execution"
        assert hf.severity == "high"
        assert hf.summary == "Test"
        assert hf.evidence == []

    def test_evidence_default_factory(self):
        hf1 = HealthFinding(
            finding_id="hf_a", category="x", severity="low", summary="a"
        )
        hf2 = HealthFinding(
            finding_id="hf_b", category="x", severity="low", summary="b"
        )
        hf1.evidence.append("item")
        assert "item" not in hf2.evidence


# =============================================================================
# ImprovementPlan dataclass
# =============================================================================


class TestImprovementPlanDataclass:
    """Tests for ImprovementPlan schema correctness."""

    def test_defaults(self):
        plan = ImprovementPlan(improvement_id="imp_1")
        assert plan.source_plan_id is None
        assert plan.findings == []
        assert plan.goals == []
        assert plan.allowed_skills == []
        assert plan.max_steps == 0
        assert plan.max_files_changed == 0
        assert plan.requires_human_review is False
        assert plan.status == "queued"


# =============================================================================
# ImprovementResult dataclass
# =============================================================================


class TestImprovementResultDataclass:
    """Tests for ImprovementResult schema correctness."""

    def test_defaults(self):
        result = ImprovementResult(
            improvement_id="imp_1",
            executed=False,
            final_status="skipped",
        )
        assert result.evaluation_grade is None
        assert result.evaluation_score is None
        assert result.followup_recommended is False
        assert result.notes == []


# =============================================================================
# Severity validation
# =============================================================================


class TestSeverityValidation:
    """Tests for deterministic severity enforcement."""

    def test_valid_severities_accepted(self):
        """All four valid severity values are accepted."""
        for sev in VALID_SEVERITIES:
            hf = HealthFinding(
                finding_id="hf_test",
                category="low_grade_execution",
                severity=sev,
                summary="Test",
            )
            assert hf.severity == sev

    def test_invalid_severity_raises(self):
        """Invalid severity value raises ValueError."""
        with pytest.raises(ValueError, match="Invalid severity"):
            HealthFinding(
                finding_id="hf_bad",
                category="low_grade_execution",
                severity="extreme",
                summary="Should fail",
            )

    def test_empty_severity_raises(self):
        """Empty string severity raises ValueError."""
        with pytest.raises(ValueError, match="Invalid severity"):
            HealthFinding(
                finding_id="hf_empty",
                category="low_grade_execution",
                severity="",
                summary="Should fail",
            )

    def test_case_sensitive_severity(self):
        """Severity is case-sensitive — 'High' is invalid."""
        with pytest.raises(ValueError, match="Invalid severity"):
            HealthFinding(
                finding_id="hf_case",
                category="low_grade_execution",
                severity="High",
                summary="Should fail",
            )

    def test_valid_severities_constant(self):
        """VALID_SEVERITIES has exactly four entries."""
        assert VALID_SEVERITIES == ("low", "medium", "high", "critical")

    def test_all_planner_severities_valid(self, planner: ImprovementPlanner):
        """All severities produced by build_health_findings are valid."""
        pe = _plan_eval(
            grade="D",
            aggregate_score=0.42,
            step_evaluations=[
                _step_eval(
                    contract_valid=False,
                    retry_penalty=0.10,
                    verification_score=0.05,
                    execution_success=True,
                    duration_score=0.0,
                ),
            ],
        )
        findings = planner.build_health_findings(pe)
        for f in findings:
            assert f.severity in VALID_SEVERITIES
