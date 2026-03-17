"""Tests for Phase 9 — Launch gate, adapter selection, readiness, rollback."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountMode, AccountState
from novatrade.monitor.ops_monitor import OpsMonitor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.dry_run import DryRunAdapter
from novatrade.runtime.launch_gate import (
    CheckCategory,
    LaunchMode,
    ReadinessVerdict,
    evaluate_launch_gate,
    generate_readiness_report,
    resolve_launch_mode,
    validate_startup,
)
from novatrade.runtime.webhook_server import WebhookState, build_status, create_app
from novatrade.validation.evidence import EvidenceRecorder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    risk = RiskConfig(
        max_daily_drawdown_pct=5.0,
        max_total_drawdown_pct=10.0,
        max_positions=5,
        max_volume_per_trade=1.0,
        min_volume_per_trade=0.01,
        check_forex_session=False,
        irb_max_open_positions=1,
    )
    defaults = dict(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=risk,
        dry_run=True,
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _build_stack(tmp_path: Path, *, launch_mode: LaunchMode = LaunchMode.DRY_RUN):
    """Build a full test stack with specified launch mode."""
    cfg = _cfg(dry_run=False)  # DryRunAdapter is the safety net
    adapter = DryRunAdapter()
    recorder = EvidenceRecorder(path=tmp_path / "evidence.jsonl")
    risk_engine = RiskEngine(cfg)
    risk_engine.initialize(AccountState(balance=100_000, equity=100_000, mode=AccountMode.DEMO))
    agent = TradingAgent(cfg=cfg, adapter=adapter, risk_engine=risk_engine, recorder=recorder)
    monitor = OpsMonitor(
        cfg=cfg,
        adapter=adapter,
        agent=agent,
        risk_engine=risk_engine,
        recorder=recorder,
    )
    ws = WebhookState(
        agent=agent,
        risk_engine=risk_engine,
        monitor=monitor,
        recorder=recorder,
        dry_run=True,
        launch_mode=launch_mode,
        adapter_type="DryRunAdapter",
        started_at=time.time(),
    )
    return ws, adapter, agent, risk_engine, monitor, recorder


# ===========================================================================
# 1. Launch mode resolution
# ===========================================================================


class TestLaunchModeResolution:
    """Tests for resolve_launch_mode()."""

    def test_default_is_dry_run(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.setdefault("NOVATRADE_DRY_RUN", "true")
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

    def test_explicit_dry_run(self):
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "dry_run"}):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

    def test_explicit_active_ready(self):
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active_ready"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY

    def test_explicit_active_demo(self):
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active_demo"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_DEMO

    def test_fallback_dry_run_true(self):
        with patch.dict(os.environ, {"NOVATRADE_DRY_RUN": "true"}, clear=True):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

    def test_fallback_dry_run_false(self):
        with patch.dict(os.environ, {"NOVATRADE_DRY_RUN": "false"}, clear=True):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY

    def test_hyphen_variants(self):
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active-ready"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active-demo"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_DEMO
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "dry-run"}):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN


# ===========================================================================
# 2. Startup validation
# ===========================================================================


class TestStartupValidation:
    """Tests for validate_startup()."""

    def test_dry_run_minimal_config(self):
        cfg = _cfg()
        result = validate_startup(cfg, LaunchMode.DRY_RUN)
        assert result.ok is True
        assert result.mode == LaunchMode.DRY_RUN

    def test_dry_run_warns_no_token(self):
        cfg = _cfg()
        cfg.metaapi.token = ""
        with patch.dict(os.environ, {}, clear=True):
            result = validate_startup(cfg, LaunchMode.DRY_RUN)
        assert result.ok is True
        assert any("METAAPI_TOKEN" in w for w in result.warnings)

    def test_active_ready_fails_without_credentials(self):
        cfg = _cfg()
        cfg.metaapi.token = ""
        cfg.metaapi.account_id = ""
        with patch.dict(os.environ, {}, clear=True):
            result = validate_startup(cfg, LaunchMode.ACTIVE_READY)
        assert result.ok is False
        assert any("METAAPI_TOKEN" in e for e in result.errors)

    def test_active_ready_fails_without_webhook_secret(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        with patch.dict(os.environ, {}, clear=True):
            result = validate_startup(cfg, LaunchMode.ACTIVE_READY)
        assert result.ok is False
        assert any("WEBHOOK_SECRET" in e for e in result.errors)

    def test_active_ready_passes_with_all_credentials(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret123"}):
            result = validate_startup(cfg, LaunchMode.ACTIVE_READY)
        assert result.ok is True

    def test_no_symbols_fails(self):
        cfg = _cfg(symbols=[])
        result = validate_startup(cfg, LaunchMode.DRY_RUN)
        assert result.ok is False
        assert any("symbol" in e for e in result.errors)

    def test_active_demo_same_as_active_ready(self):
        cfg = _cfg()
        cfg.metaapi.token = ""
        with patch.dict(os.environ, {}, clear=True):
            result = validate_startup(cfg, LaunchMode.ACTIVE_DEMO)
        assert result.ok is False


# ===========================================================================
# 3. Launch gate evaluation
# ===========================================================================


class TestLaunchGateEvaluation:
    """Tests for evaluate_launch_gate()."""

    def test_dry_run_ready(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=True,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        assert result.verdict == ReadinessVerdict.READY_FOR_ACTIVE_DEMO
        assert result.launch_mode == LaunchMode.DRY_RUN

    def test_dry_run_not_ready_if_agent_missing(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=False,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        assert result.verdict == ReadinessVerdict.NOT_READY
        assert len(result.blockers) > 0

    def test_active_ready_conditional_without_confirmations(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_READY,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )
        assert result.verdict == ReadinessVerdict.CONDITIONALLY_READY
        assert len(result.external_confirmations) > 0

    def test_active_demo_not_ready_without_confirmations(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_DEMO,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )
        assert result.verdict == ReadinessVerdict.NOT_READY
        assert len(result.blockers) > 0

    def test_active_demo_ready_with_all_confirmations(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        env = {
            "NOVATRADE_WEBHOOK_SECRET": "secret",
            "NOVATRADE_CONFIRM_PINE_COMPILED": "true",
            "NOVATRADE_CONFIRM_TV_BACKTEST": "true",
            "NOVATRADE_CONFIRM_WEBHOOK_URL": "true",
            "NOVATRADE_CONFIRM_ACTIVE_DEMO": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_DEMO,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )
        assert result.verdict == ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    def test_risk_halted_blocks_launch(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        env = {
            "NOVATRADE_WEBHOOK_SECRET": "secret",
            "NOVATRADE_CONFIRM_PINE_COMPILED": "true",
            "NOVATRADE_CONFIRM_TV_BACKTEST": "true",
            "NOVATRADE_CONFIRM_WEBHOOK_URL": "true",
            "NOVATRADE_CONFIRM_ACTIVE_DEMO": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_DEMO,
                risk_engine_initialized=True,
                risk_engine_halted=True,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )
        assert result.verdict == ReadinessVerdict.NOT_READY
        assert any("halted" in b.lower() for b in result.blockers)

    def test_dry_run_adapter_blocks_active_mode(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_READY,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="DryRunAdapter",
            )
        # Should fail because DryRunAdapter is not an active adapter
        assert result.verdict != ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    def test_checks_have_categories(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=True,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        categories = {c.category for c in result.checks}
        assert CheckCategory.CODE in categories
        assert CheckCategory.RISK in categories


# ===========================================================================
# 4. Readiness report generation
# ===========================================================================


class TestReadinessReport:
    """Tests for generate_readiness_report()."""

    def test_report_contains_verdict(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=True,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        report = generate_readiness_report(result)
        assert "READY_FOR_ACTIVE_DEMO" in report
        assert "dry_run" in report

    def test_report_shows_blockers(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=False,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        report = generate_readiness_report(result)
        assert "BLOCK" in report

    def test_to_dict_structure(self):
        cfg = _cfg()
        result = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=True,
            monitor_initialized=True,
            adapter_connected=True,
            adapter_type="DryRunAdapter",
        )
        d = result.to_dict()
        assert "verdict" in d
        assert "launch_mode" in d
        assert "checks" in d
        assert "passed" in d
        assert "failed" in d
        assert "total" in d


# ===========================================================================
# 5. Runner build_stack
# ===========================================================================


class TestRunnerBuildStack:
    """Tests for the updated runner.build_stack()."""

    def test_build_stack_dry_run(self):
        from novatrade.runtime.runner import build_stack

        cfg = _cfg(dry_run=True)
        ws, loop, readiness = build_stack(cfg, mode=LaunchMode.DRY_RUN)
        assert ws.dry_run is True
        assert ws.launch_mode == LaunchMode.DRY_RUN
        assert ws.adapter_type == "DryRunAdapter"
        assert ws.agent is not None
        assert ws.risk_engine is not None
        assert readiness.verdict == ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    def test_build_stack_forces_dry_run_adapter(self):
        """In dry_run mode, adapter is always DryRunAdapter."""
        from novatrade.runtime.runner import build_stack

        cfg = _cfg(dry_run=True)
        ws, loop, readiness = build_stack(cfg, mode=LaunchMode.DRY_RUN)
        assert isinstance(ws.agent._adapter, DryRunAdapter)

    def test_build_stack_active_ready_fails_without_credentials(self):
        """active_ready mode fails explicitly without MetaApi credentials."""
        from novatrade.runtime.runner import build_stack

        cfg = _cfg(dry_run=False)
        cfg.metaapi.token = ""
        cfg.metaapi.account_id = ""
        with pytest.raises(RuntimeError, match="METAAPI_TOKEN"):
            build_stack(cfg, mode=LaunchMode.ACTIVE_READY)

    def test_build_stack_active_demo_fails_without_confirmations(self):
        """active_demo mode fails without operator confirmations."""
        from novatrade.runtime.runner import build_stack

        cfg = _cfg(dry_run=False)
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        # Mock the MetaApiAdapter import to avoid real SDK dependency
        with (
            patch("novatrade.runtime.runner._create_adapter") as mock_create,
            patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True),
        ):
            mock_adapter = DryRunAdapter()
            mock_create.return_value = mock_adapter
            with pytest.raises(RuntimeError, match="launch gate"):
                build_stack(cfg, mode=LaunchMode.ACTIVE_DEMO)

    def test_build_stack_records_startup_events(self, tmp_path):
        from novatrade.runtime.runner import build_stack

        cfg = _cfg(dry_run=True, data_dir=tmp_path)
        ws, loop, readiness = build_stack(cfg, mode=LaunchMode.DRY_RUN)
        records = ws.recorder.load()
        events = [r.data.get("event") for r in records]
        assert "STARTUP_VALIDATION" in events
        assert "LAUNCH_GATE_EVALUATED" in events


# ===========================================================================
# 6. Adapter selection
# ===========================================================================


class TestAdapterSelection:
    """Tests for adapter selection logic."""

    def test_dry_run_uses_dry_adapter(self):
        from novatrade.runtime.runner import _create_adapter

        cfg = _cfg()
        adapter = _create_adapter(cfg, LaunchMode.DRY_RUN)
        assert isinstance(adapter, DryRunAdapter)

    def test_active_ready_fails_without_token(self):
        from novatrade.runtime.runner import _create_adapter

        cfg = _cfg()
        cfg.metaapi.token = ""
        with pytest.raises(RuntimeError, match="missing credentials"):
            _create_adapter(cfg, LaunchMode.ACTIVE_READY)

    def test_active_demo_fails_without_token(self):
        from novatrade.runtime.runner import _create_adapter

        cfg = _cfg()
        cfg.metaapi.token = ""
        with pytest.raises(RuntimeError, match="missing credentials"):
            _create_adapter(cfg, LaunchMode.ACTIVE_DEMO)


# ===========================================================================
# 7. Rollback to dry-run
# ===========================================================================


class TestRollback:
    """Tests for rollback_to_dry_run()."""

    def test_rollback_switches_adapter(self, tmp_path):
        from novatrade.runtime.runner import rollback_to_dry_run

        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path, launch_mode=LaunchMode.ACTIVE_READY)
        ws.dry_run = False
        ws.launch_mode = LaunchMode.ACTIVE_READY
        ws.adapter_type = "MetaApiAdapter"

        rollback_to_dry_run(ws, recorder)

        assert ws.dry_run is True
        assert ws.launch_mode == LaunchMode.DRY_RUN
        assert ws.adapter_type == "DryRunAdapter"
        assert isinstance(ws.agent._adapter, DryRunAdapter)

    def test_rollback_records_evidence(self, tmp_path):
        from novatrade.runtime.runner import rollback_to_dry_run

        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        rollback_to_dry_run(ws, recorder)
        records = recorder.load()
        events = [r.data.get("event") for r in records]
        assert "ROLLBACK_TO_DRY_RUN" in events


# ===========================================================================
# 8. Webhook server launch-mode endpoints
# ===========================================================================


class TestWebhookLaunchMode:
    """Tests for launch-mode-aware webhook endpoints."""

    def test_health_includes_launch_mode(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        assert data["launch_mode"] == "dry_run"
        assert data["adapter_type"] == "DryRunAdapter"

    def test_status_includes_launch_mode(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/status")
        data = resp.json()
        assert data["runtime_mode"] == "dry_run"
        assert data["adapter_type"] == "DryRunAdapter"

    def test_readiness_endpoint(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert "verdict" in data
        assert "checks" in data
        assert "launch_mode" in data

    def test_rollback_endpoint(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        ws.launch_mode = LaunchMode.ACTIVE_READY
        ws.dry_run = False
        ws.adapter_type = "MetaApiAdapter"
        app = create_app(ws)
        client = TestClient(app)

        resp = client.post("/control/rollback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["dry_run"] is True
        assert data["launch_mode"] == "dry_run"
        assert data["adapter_type"] == "DryRunAdapter"

    def test_status_after_rollback(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        ws.launch_mode = LaunchMode.ACTIVE_READY
        ws.dry_run = False
        app = create_app(ws)
        client = TestClient(app)

        # Rollback
        client.post("/control/rollback")

        # Status should reflect dry-run
        resp = client.get("/status")
        data = resp.json()
        assert data["runtime_mode"] == "dry_run"
        assert data["dry_run"] is True

    def test_build_status_launch_mode(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        status = build_status(ws)
        assert status["runtime_mode"] == "dry_run"
        assert status["adapter_type"] == "DryRunAdapter"

    def test_build_status_active_mode(self, tmp_path):
        ws, *_ = _build_stack(tmp_path, launch_mode=LaunchMode.ACTIVE_READY)
        ws.dry_run = False
        ws.adapter_type = "MetaApiAdapter"
        status = build_status(ws)
        assert status["runtime_mode"] == "active_ready"
        assert status["adapter_type"] == "MetaApiAdapter"
