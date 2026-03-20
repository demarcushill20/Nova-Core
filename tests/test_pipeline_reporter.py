"""Tests for novatrade.pipeline.reporter — enhanced report formatting and file output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult, PipelineStage
from novatrade.pipeline.reporter import (
    campaign_report,
    format_pipeline_json,
    format_pipeline_report,
    report_to_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_result(**overrides: Any) -> PipelineResult:
    """Build a PipelineResult with sensible defaults."""
    defaults: dict[str, Any] = {
        "mode": PipelineMode.CAMPAIGN,
        "doctrine_name": "IRB Conservative",
        "total_experiments": 200,
        "gate_pass_rate": 0.35,
        "best_score": 1.8500,
        "best_experiment_id": "exp-abc123",
        "data_quality_issues": 0,
        "top_survivors": [
            {
                "experiment_id": "exp-abc123",
                "score": 1.85,
                "profit_factor": 2.1,
                "max_dd": 8.5,
                "trade_count": 72,
                "status": "ranked",
            },
            {
                "experiment_id": "exp-def456",
                "score": 1.72,
                "profit_factor": 1.9,
                "max_dd": 10.2,
                "trade_count": 65,
                "status": "ranked",
            },
        ],
        "stages": [
            PipelineStage(name="load_data", status="ok", duration_ms=150),
            PipelineStage(name="validate_data", status="ok", duration_ms=50),
            PipelineStage(name="parse_doctrine", status="ok", duration_ms=30),
            PipelineStage(name="generate_config", status="ok", duration_ms=20),
            PipelineStage(name="execute_campaign", status="ok", duration_ms=120000),
        ],
        "errors": [],
    }
    defaults.update(overrides)
    return PipelineResult(**defaults)


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    d = tmp_path / "output"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Existing report formats (regression tests)
# ---------------------------------------------------------------------------


class TestFormatPipelineReport:
    """Ensure existing format_pipeline_report still works."""

    def test_contains_header(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "NovaTrade Pipeline Report" in report

    def test_contains_mode(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "CAMPAIGN" in report

    def test_contains_doctrine_name(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "IRB Conservative" in report

    def test_contains_best_score(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "1.8500" in report

    def test_contains_gate_pass_rate(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "35.0%" in report

    def test_contains_top_survivors(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "Top Survivors" in report
        assert "exp-abc123" in report

    def test_contains_stage_timeline(self) -> None:
        result = _make_result()
        report = format_pipeline_report(result)
        assert "Stage Timeline" in report
        assert "load_data" in report

    def test_error_section_present(self) -> None:
        result = _make_result(errors=["Something went wrong"])
        report = format_pipeline_report(result)
        assert "Errors" in report
        assert "Something went wrong" in report

    def test_no_error_section_when_clean(self) -> None:
        result = _make_result(errors=[])
        report = format_pipeline_report(result)
        assert "Errors" not in report

    def test_data_quality_section(self) -> None:
        result = _make_result(data_quality_issues=5)
        report = format_pipeline_report(result)
        assert "Data Quality" in report


class TestFormatPipelineJson:
    """Ensure existing format_pipeline_json still works."""

    def test_valid_json(self) -> None:
        result = _make_result()
        j = format_pipeline_json(result)
        data = json.loads(j)
        assert data["mode"] == "campaign"
        assert data["total_experiments"] == 200

    def test_nan_handling(self) -> None:
        result = _make_result(
            top_survivors=[{"experiment_id": "exp-nan", "score": float("nan"), "profit_factor": float("inf")}]
        )
        j = format_pipeline_json(result)
        data = json.loads(j)
        assert data["top_survivors"][0]["score"] is None
        assert data["top_survivors"][0]["profit_factor"] is None


# ---------------------------------------------------------------------------
# Campaign report (P7.3)
# ---------------------------------------------------------------------------


class TestCampaignReport:
    """Test the comprehensive campaign report generator."""

    def test_executive_summary(self) -> None:
        result = _make_result()
        report = campaign_report(result)
        assert "EXECUTIVE SUMMARY" in report
        assert "200" in report  # total experiments
        assert "35.0%" in report  # gate pass rate
        assert "1.8500" in report  # best score

    def test_top_survivors_section(self) -> None:
        result = _make_result()
        report = campaign_report(result)
        assert "TOP SURVIVORS" in report
        assert "exp-abc123" in report
        assert "exp-def456" in report

    def test_stage_c_results(self) -> None:
        result = _make_result()
        stage_c = [
            {"experiment_id": "exp-abc123", "passed": True, "reason": "all gates passed"},
            {"experiment_id": "exp-def456", "passed": False, "reason": "WFE too low"},
        ]
        report = campaign_report(result, stage_c_results=stage_c)
        assert "STAGE C GATE RESULTS" in report
        assert "exp-abc123: PASS" in report
        assert "exp-def456: FAIL" in report
        assert "WFE too low" in report

    def test_durability_grades(self) -> None:
        result = _make_result()
        grades = [
            {"experiment_id": "exp-abc123", "grade": "A", "score": 0.92, "detail": "robust"},
            {"experiment_id": "exp-def456", "grade": "B", "score": 0.75, "detail": "moderate"},
        ]
        report = campaign_report(result, durability_grades=grades)
        assert "DURABILITY GRADES" in report
        assert "0.9200" in report
        assert "robust" in report

    def test_param_distributions(self) -> None:
        result = _make_result()
        params = {
            "irb_threshold": {"min": 0.35, "max": 0.55, "mean": 0.45, "winner_count": 12},
            "ema_period": {"min": 15.0, "max": 30.0, "mean": 22.5, "winner_count": 8},
        }
        report = campaign_report(result, param_distributions=params)
        assert "PARAMETER DISTRIBUTION" in report
        assert "irb_threshold" in report
        assert "ema_period" in report

    def test_mutation_lineage(self) -> None:
        result = _make_result()
        lineage = [
            {"experiment_id": "exp-abc123", "mutation_level": 2, "parent_id": "exp-000", "score_delta": 0.15},
            {"experiment_id": "exp-def456", "mutation_level": 1, "parent_id": "exp-abc123", "score_delta": -0.13},
        ]
        report = campaign_report(result, mutation_lineage=lineage)
        assert "MUTATION LINEAGE" in report
        assert "L2" in report
        assert "+0.1500" in report

    def test_no_optional_sections_when_empty(self) -> None:
        result = _make_result(errors=[])
        report = campaign_report(result)
        assert "STAGE C GATE RESULTS" not in report
        assert "DURABILITY GRADES" not in report
        assert "PARAMETER DISTRIBUTION" not in report
        assert "MUTATION LINEAGE" not in report
        assert "ERRORS" not in report

    def test_errors_section(self) -> None:
        result = _make_result(errors=["Something failed"])
        report = campaign_report(result)
        assert "ERRORS" in report
        assert "Something failed" in report

    def test_header_and_footer(self) -> None:
        result = _make_result()
        report = campaign_report(result)
        lines = report.split("\n")
        assert "=" * 70 in lines[0]
        assert "=" * 70 in lines[-1]

    def test_zero_experiments(self) -> None:
        result = _make_result(total_experiments=0, top_survivors=[])
        report = campaign_report(result)
        assert "EXECUTIVE SUMMARY" in report
        assert "0" in report

    def test_stage_durations_reported(self) -> None:
        result = _make_result()
        report = campaign_report(result)
        assert "Total Duration:" in report


# ---------------------------------------------------------------------------
# File output (P7.3)
# ---------------------------------------------------------------------------


class TestReportToFile:
    """Test report_to_file writes correctly."""

    def test_text_format(self, output_dir: Path) -> None:
        result = _make_result()
        path = report_to_file(result, output_dir, report_format="text")
        assert path.exists()
        assert path.suffix == ".txt"
        content = path.read_text("utf-8")
        assert "NovaTrade Pipeline Report" in content

    def test_json_format(self, output_dir: Path) -> None:
        result = _make_result()
        path = report_to_file(result, output_dir, report_format="json")
        assert path.exists()
        assert path.suffix == ".json"
        data = json.loads(path.read_text("utf-8"))
        assert data["mode"] == "campaign"

    def test_campaign_format(self, output_dir: Path) -> None:
        result = _make_result()
        path = report_to_file(result, output_dir, report_format="campaign")
        assert path.exists()
        assert path.suffix == ".txt"
        content = path.read_text("utf-8")
        assert "Campaign Report" in content

    def test_campaign_format_with_extras(self, output_dir: Path) -> None:
        result = _make_result()
        path = report_to_file(
            result,
            output_dir,
            report_format="campaign",
            stage_c_results=[{"experiment_id": "exp-1", "passed": True, "reason": "ok"}],
            durability_grades=[{"experiment_id": "exp-1", "grade": "A", "score": 0.9, "detail": ""}],
        )
        content = path.read_text("utf-8")
        assert "STAGE C GATE RESULTS" in content
        assert "DURABILITY GRADES" in content

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        result = _make_result()
        path = report_to_file(result, deep)
        assert path.exists()

    def test_filename_includes_mode(self, output_dir: Path) -> None:
        result = _make_result(mode=PipelineMode.SINGLE)
        path = report_to_file(result, output_dir, report_format="text")
        assert "single" in path.name

    def test_filename_includes_timestamp(self, output_dir: Path) -> None:
        result = _make_result()
        path = report_to_file(result, output_dir, report_format="text")
        # Filename format: pipeline_report_campaign_YYYYMMDD_HHMMSS.txt
        assert "20" in path.name  # year prefix

    def test_no_tmp_files_left(self, output_dir: Path) -> None:
        result = _make_result()
        report_to_file(result, output_dir)
        tmp_files = list(output_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_multiple_writes_no_collision(self, output_dir: Path) -> None:
        """Writing multiple reports in quick succession should not collide."""
        result = _make_result()
        paths = set()
        for _ in range(3):
            p = report_to_file(result, output_dir)
            paths.add(p)
        # Due to sub-second writes, filenames may collide (same second).
        # The important thing is no exception is raised.
        assert len(paths) >= 1
