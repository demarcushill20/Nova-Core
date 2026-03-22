"""Tests for two CRITICAL bug fixes:

Bug 1 — Stage B realism gate defaults must reject zero-cost backtests.
Bug 2 — Atomic write helpers must not double-close the file descriptor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.evaluation.gates import (
    GateConfig,
    GateResults,
    evaluate_stage_b,
)

# ── Bug 1: Stage B realism gate defaults ─────────────────────────────────


class TestRealismGateDefaults:
    """Default GateConfig must reject zero-cost environments."""

    def test_default_min_spread_is_half_pip(self):
        cfg = GateConfig()
        assert cfg.min_avg_spread_pips == 0.5

    def test_default_min_slippage_is_0_1_pip(self):
        cfg = GateConfig()
        assert cfg.min_slippage_pips == 0.1

    def test_default_min_commission_is_2_usd(self):
        cfg = GateConfig()
        assert cfg.min_commission_per_lot == 2.0

    def test_zero_cost_env_fails_all_realism_gates(self):
        """A completely zero-cost environment must fail spread, slippage, AND commission."""
        env = {
            "avg_spread_pips": 0.0,
            "slippage_pips": 0.0,
            "commission_per_lot_usd": 0.0,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env)
        gate_results = GateResults(results=results)

        assert not gate_results.passed, "Zero-cost env must fail Stage B"

        spread = next(r for r in results if r.gate == "B.spread_applied")
        slip = next(r for r in results if r.gate == "B.slippage_applied")
        comm = next(r for r in results if r.gate == "B.commission_applied")

        assert not spread.passed, "Zero spread must fail"
        assert not slip.passed, "Zero slippage must fail"
        assert not comm.passed, "Zero commission must fail"

    def test_zero_commission_alone_fails(self):
        """Even with good spread/slippage, zero commission must fail."""
        env = {
            "avg_spread_pips": 1.5,
            "slippage_pips": 0.5,
            "commission_per_lot_usd": 0.0,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env)
        comm = next(r for r in results if r.gate == "B.commission_applied")
        assert not comm.passed, "Zero commission must fail default gate"

    def test_realistic_env_passes_all_gates(self):
        """Typical ECN environment should pass all Stage B gates."""
        env = {
            "avg_spread_pips": 1.5,
            "slippage_pips": 0.2,
            "commission_per_lot_usd": 5.0,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env)
        gate_results = GateResults(results=results)
        assert gate_results.passed, "Realistic ECN costs must pass Stage B"

    def test_boundary_values_pass(self):
        """Exact boundary values (>= comparison) should pass."""
        env = {
            "avg_spread_pips": 0.5,
            "slippage_pips": 0.1,
            "commission_per_lot_usd": 2.0,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env)
        gate_results = GateResults(results=results)
        assert gate_results.passed, "Exact boundary values must pass"

    def test_just_below_boundary_fails(self):
        """Values slightly below the floor must fail."""
        env = {
            "avg_spread_pips": 0.49,
            "slippage_pips": 0.09,
            "commission_per_lot_usd": 1.99,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env)
        gate_results = GateResults(results=results)
        assert not gate_results.passed, "Below-boundary values must fail"

    def test_custom_config_can_override_defaults(self):
        """Operators can relax the gate with custom GateConfig (e.g. spread-embedded broker)."""
        relaxed = GateConfig(
            min_avg_spread_pips=0.5,
            min_slippage_pips=0.05,
            min_commission_per_lot=0.0,
        )
        env = {
            "avg_spread_pips": 0.5,
            "slippage_pips": 0.05,
            "commission_per_lot_usd": 0.0,
            "stop_first_on_ambiguity": True,
        }
        results = evaluate_stage_b(env, config=relaxed)
        gate_results = GateResults(results=results)
        assert gate_results.passed, "Custom relaxed config should pass"


# ── Bug 2: Double os.close() in atomic writes ────────────────────────────


class TestAtomicWriteNoDoubleClose:
    """Atomic write must call os.close(fd) exactly once, even on os.replace() failure."""

    def test_campaign_checkpoint_no_double_close(self, tmp_path: Path):
        """save_checkpoint must not double-close the fd when os.replace fails."""
        from novatrade.optimization.campaign_engine import (
            CampaignCheckpoint,
            save_checkpoint,
        )

        cp = CampaignCheckpoint(
            campaign_id="test-camp",
            experiment_count=1,
            best_experiment_id="exp-001",
            best_scout_score=0.5,
            best_config={"a": 1},
            stagnation_count=0,
            crash_count=0,
            elapsed_seconds=10.0,
        )

        close_calls: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            original_close(fd)

        with (
            patch("novatrade.optimization.campaign_engine.os.close", side_effect=tracking_close),
            patch(
                "novatrade.optimization.campaign_engine.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            save_checkpoint(cp, tmp_path)

        # The fd should appear exactly once in close_calls
        assert len(close_calls) == 1, f"os.close called {len(close_calls)} times, expected 1. fds: {close_calls}"

    def test_campaign_checkpoint_happy_path(self, tmp_path: Path):
        """Normal save_checkpoint should work correctly after the fix."""
        from novatrade.optimization.campaign_engine import (
            CampaignCheckpoint,
            save_checkpoint,
        )

        cp = CampaignCheckpoint(
            campaign_id="test-camp",
            experiment_count=1,
            best_experiment_id="exp-001",
            best_scout_score=0.5,
            best_config={"a": 1},
            stagnation_count=0,
            crash_count=0,
            elapsed_seconds=10.0,
        )
        path = save_checkpoint(cp, tmp_path)
        data = json.loads(path.read_text())
        assert data["campaign_id"] == "test-camp"
        assert data["best_scout_score"] == 0.5

    def test_artifacts_atomic_write_no_double_close(self, tmp_path: Path):
        """_atomic_write in artifacts.py must not double-close on os.replace failure."""
        from novatrade.storage.artifacts import _atomic_write

        out_path = tmp_path / "test.json"

        close_calls: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            original_close(fd)

        with (
            patch("novatrade.storage.artifacts.os.close", side_effect=tracking_close),
            patch(
                "novatrade.storage.artifacts.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            _atomic_write(out_path, '{"key": "value"}')

        assert len(close_calls) == 1, f"os.close called {len(close_calls)} times, expected 1. fds: {close_calls}"

    def test_artifacts_atomic_write_happy_path(self, tmp_path: Path):
        """Normal _atomic_write should produce the expected file."""
        from novatrade.storage.artifacts import _atomic_write

        out_path = tmp_path / "test.json"
        _atomic_write(out_path, '{"key": "value"}')
        assert json.loads(out_path.read_text()) == {"key": "value"}

    def test_artifacts_atomic_write_cleans_tmp_on_write_failure(self, tmp_path: Path):
        """If os.write fails, fd is closed and tmp file cleaned up."""
        from novatrade.storage.artifacts import _atomic_write

        out_path = tmp_path / "test.json"

        with (
            patch(
                "novatrade.storage.artifacts.os.write",
                side_effect=OSError("write error"),
            ),
            pytest.raises(OSError, match="write error"),
        ):
            _atomic_write(out_path, '{"key": "value"}')

        # No .tmp files should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Leftover tmp files: {tmp_files}"

    # ── experiment_db.export_tsv ──────────────────────────────────────────

    def test_experiment_db_export_tsv_no_double_close(self, tmp_path: Path):
        """export_tsv must not double-close the fd when os.replace fails."""
        from novatrade.storage.experiment_db import ExperimentDB

        db = ExperimentDB(tmp_path / "test.db")
        out_path = tmp_path / "export.tsv"

        close_calls: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            original_close(fd)

        with (
            patch("novatrade.storage.experiment_db.os.close", side_effect=tracking_close),
            patch(
                "novatrade.storage.experiment_db.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            db.export_tsv("campaign-x", out_path)

        assert len(close_calls) == 1, f"os.close called {len(close_calls)} times, expected 1. fds: {close_calls}"

    def test_experiment_db_export_tsv_happy_path(self, tmp_path: Path):
        """Normal export_tsv should produce a valid TSV file."""
        from novatrade.storage.experiment_db import ExperimentDB

        db = ExperimentDB(tmp_path / "test.db")
        out_path = tmp_path / "export.tsv"
        count = db.export_tsv("campaign-x", out_path)
        assert count == 0
        assert out_path.exists()
        content = out_path.read_text()
        assert "experiment_id" in content  # header row present

    # ── pipeline/checkpoint.save_checkpoint ───────────────────────────────

    def test_pipeline_checkpoint_no_double_close(self, tmp_path: Path):
        """pipeline checkpoint save must not double-close fd on os.replace failure."""
        from novatrade.pipeline.checkpoint import (
            CampaignCheckpoint as PipelineCheckpoint,
        )
        from novatrade.pipeline.checkpoint import (
            save_checkpoint as pipeline_save_checkpoint,
        )

        cp = PipelineCheckpoint(campaign_id="test-pipe")

        close_calls: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            original_close(fd)

        with (
            patch("novatrade.pipeline.checkpoint.os.close", side_effect=tracking_close),
            patch(
                "novatrade.pipeline.checkpoint.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            pipeline_save_checkpoint(cp, tmp_path)

        assert len(close_calls) == 1, f"os.close called {len(close_calls)} times, expected 1. fds: {close_calls}"

    def test_pipeline_checkpoint_happy_path(self, tmp_path: Path):
        """Normal pipeline checkpoint save should produce a valid JSON file."""
        from novatrade.pipeline.checkpoint import (
            CampaignCheckpoint as PipelineCheckpoint,
        )
        from novatrade.pipeline.checkpoint import (
            save_checkpoint as pipeline_save_checkpoint,
        )

        cp = PipelineCheckpoint(campaign_id="test-pipe", best_score=1.5)
        path = pipeline_save_checkpoint(cp, tmp_path)
        data = json.loads(path.read_text())
        assert data["campaign_id"] == "test-pipe"
        assert data["best_score"] == 1.5

    # ── pipeline/reporter.report_to_file ──────────────────────────────────

    def test_reporter_no_double_close(self, tmp_path: Path):
        """report_to_file must not double-close fd on os.replace failure."""
        from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult
        from novatrade.pipeline.reporter import report_to_file

        result = PipelineResult(mode=PipelineMode.SINGLE)

        close_calls: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            original_close(fd)

        with (
            patch("novatrade.pipeline.reporter.os.close", side_effect=tracking_close),
            patch(
                "novatrade.pipeline.reporter.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            report_to_file(result, tmp_path)

        assert len(close_calls) == 1, f"os.close called {len(close_calls)} times, expected 1. fds: {close_calls}"

    def test_reporter_happy_path(self, tmp_path: Path):
        """Normal report_to_file should produce a report file."""
        from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult
        from novatrade.pipeline.reporter import report_to_file

        result = PipelineResult(mode=PipelineMode.SINGLE)
        path = report_to_file(result, tmp_path)
        assert path.exists()
        assert path.stat().st_size > 0

    # ── Original error propagation ────────────────────────────────────────

    def test_original_error_propagates_not_masked(self, tmp_path: Path):
        """When os.replace fails, the ORIGINAL error must propagate, not an OSError from double-close."""
        from novatrade.storage.artifacts import _atomic_write

        out_path = tmp_path / "test.json"

        with (
            pytest.raises(PermissionError, match="permission denied"),
            patch(
                "novatrade.storage.artifacts.os.replace",
                side_effect=PermissionError("permission denied"),
            ),
        ):
            _atomic_write(out_path, '{"key": "value"}')
