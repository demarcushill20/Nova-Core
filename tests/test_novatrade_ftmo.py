"""Tests for FTMO Free Trial configuration, preflight, and evidence tagging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.config import FtmoProfile, MetaApiConfig, NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    ExecutionOutcome,
    ExecutionResult,
    HealthState,
    HealthStatus,
    SymbolPrice,
)
from novatrade.validation.evidence import EvidenceRecorder
from novatrade.validation.ftmo_preflight import (
    CheckStatus,
    PreflightResult,
    check_config,
    format_preflight,
)

# ---------------------------------------------------------------------------
# FtmoProfile config
# ---------------------------------------------------------------------------


class TestFtmoProfile:
    def test_defaults(self):
        fp = FtmoProfile()
        assert fp.enabled is False
        assert fp.broker_label == "FTMO"
        assert fp.challenge_type == "free_trial"
        assert fp.symbol_suffix == ""
        assert fp.symbol_map == {}

    def test_resolve_passthrough(self):
        fp = FtmoProfile(enabled=True)
        assert fp.resolve_symbol("EURUSD") == "EURUSD"

    def test_resolve_suffix(self):
        fp = FtmoProfile(enabled=True, symbol_suffix=".ftmo")
        assert fp.resolve_symbol("EURUSD") == "EURUSD.ftmo"
        assert fp.resolve_symbol("GBPUSD") == "GBPUSD.ftmo"

    def test_resolve_explicit_map(self):
        fp = FtmoProfile(enabled=True, symbol_map={"EURUSD": "EURUSDm"})
        assert fp.resolve_symbol("EURUSD") == "EURUSDm"

    def test_explicit_map_takes_priority_over_suffix(self):
        fp = FtmoProfile(
            enabled=True,
            symbol_suffix=".ftmo",
            symbol_map={"EURUSD": "EURUSDm"},
        )
        assert fp.resolve_symbol("EURUSD") == "EURUSDm"
        assert fp.resolve_symbol("GBPUSD") == "GBPUSD.ftmo"

    def test_from_env_disabled(self, monkeypatch):
        monkeypatch.delenv("FTMO_ENABLED", raising=False)
        fp = FtmoProfile.from_env()
        assert fp.enabled is False

    def test_from_env_enabled(self, monkeypatch):
        monkeypatch.setenv("FTMO_ENABLED", "true")
        monkeypatch.setenv("FTMO_BROKER_LABEL", "FTMO-TEST")
        monkeypatch.setenv("FTMO_CHALLENGE_TYPE", "free_trial")
        monkeypatch.setenv("FTMO_CAMPAIGN_LABEL", "march-demo")
        monkeypatch.setenv("FTMO_SYMBOL_SUFFIX", ".ftmo")
        monkeypatch.setenv("FTMO_ACCOUNT_SIZE", "10000")
        fp = FtmoProfile.from_env()
        assert fp.enabled is True
        assert fp.broker_label == "FTMO-TEST"
        assert fp.campaign_label == "march-demo"
        assert fp.symbol_suffix == ".ftmo"
        assert fp.account_size == 10000

    def test_from_env_symbol_map(self, monkeypatch):
        monkeypatch.setenv("FTMO_ENABLED", "true")
        monkeypatch.setenv("FTMO_SYMBOL_MAP", "EURUSD:EURUSDm, GBPUSD:GBPUSDm")
        fp = FtmoProfile.from_env()
        assert fp.symbol_map == {"EURUSD": "EURUSDm", "GBPUSD": "GBPUSDm"}

    def test_from_env_empty_map(self, monkeypatch):
        monkeypatch.setenv("FTMO_ENABLED", "true")
        monkeypatch.delenv("FTMO_SYMBOL_MAP", raising=False)
        fp = FtmoProfile.from_env()
        assert fp.symbol_map == {}


# ---------------------------------------------------------------------------
# NovaTradeCfg with FTMO
# ---------------------------------------------------------------------------


class TestCfgWithFtmo:
    def test_cfg_includes_ftmo(self):
        cfg = NovaTradeCfg()
        assert isinstance(cfg.ftmo, FtmoProfile)
        assert cfg.ftmo.enabled is False

    def test_cfg_load_with_ftmo(self, monkeypatch, tmp_path):
        env = tmp_path / "test.env"
        env.write_text("METAAPI_TOKEN=tok\nMETAAPI_ACCOUNT_ID=acc\nFTMO_ENABLED=true\nFTMO_CAMPAIGN_LABEL=test-run\n")
        # Clear env first
        for key in [
            "METAAPI_TOKEN",
            "METAAPI_ACCOUNT_ID",
            "FTMO_ENABLED",
            "FTMO_CAMPAIGN_LABEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        cfg = NovaTradeCfg.load(env_file=env)
        assert cfg.ftmo.enabled is True
        assert cfg.ftmo.campaign_label == "test-run"


# ---------------------------------------------------------------------------
# Preflight config checks
# ---------------------------------------------------------------------------


class TestPreflightConfigChecks:
    def test_clean_config_passes(self):
        cfg = NovaTradeCfg(
            ftmo=FtmoProfile(enabled=True),
            metaapi=MetaApiConfig(token="tok", account_id="acc"),
            symbols=["EURUSD"],
            dry_run=True,
        )
        checks = check_config(cfg)
        statuses = {c.name: c.status for c in checks}
        assert statuses["config_valid"] == CheckStatus.PASS
        assert statuses["ftmo_enabled"] == CheckStatus.PASS
        assert statuses["mode_demo"] == CheckStatus.PASS
        assert statuses["dry_run_safe"] == CheckStatus.PASS
        assert statuses["symbols_configured"] == CheckStatus.PASS
        assert statuses["risk_stop_loss"] == CheckStatus.PASS
        assert statuses["risk_drawdown"] == CheckStatus.PASS

    def test_ftmo_disabled_warns(self):
        cfg = NovaTradeCfg()
        checks = check_config(cfg)
        ftmo_check = next(c for c in checks if c.name == "ftmo_enabled")
        assert ftmo_check.status == CheckStatus.WARN

    def test_non_demo_mode_fails(self):
        cfg = NovaTradeCfg(mode=AccountMode.CHALLENGE)
        checks = check_config(cfg)
        mode_check = next(c for c in checks if c.name == "mode_demo")
        assert mode_check.status == CheckStatus.FAIL

    def test_dry_run_false_warns(self):
        cfg = NovaTradeCfg(dry_run=False)
        checks = check_config(cfg)
        dr_check = next(c for c in checks if c.name == "dry_run_safe")
        assert dr_check.status == CheckStatus.WARN

    def test_no_symbols_fails(self):
        cfg = NovaTradeCfg(symbols=[])
        checks = check_config(cfg)
        sym_check = next(c for c in checks if c.name == "symbols_configured")
        assert sym_check.status == CheckStatus.FAIL

    def test_no_stop_loss_warns(self):
        cfg = NovaTradeCfg(risk=RiskConfig(require_stop_loss=False))
        checks = check_config(cfg)
        sl_check = next(c for c in checks if c.name == "risk_stop_loss")
        assert sl_check.status == CheckStatus.WARN

    def test_high_drawdown_warns(self):
        cfg = NovaTradeCfg(risk=RiskConfig(max_daily_drawdown_pct=8.0))
        checks = check_config(cfg)
        dd_check = next(c for c in checks if c.name == "risk_drawdown")
        assert dd_check.status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# PreflightResult properties
# ---------------------------------------------------------------------------


class TestPreflightResult:
    def test_all_pass(self):
        r = PreflightResult(
            checks=[
                MagicMock(status=CheckStatus.PASS),
                MagicMock(status=CheckStatus.PASS),
            ]
        )
        assert r.passed is True
        assert r.pass_count == 2
        assert r.fail_count == 0

    def test_warn_still_passes(self):
        r = PreflightResult(
            checks=[
                MagicMock(status=CheckStatus.PASS),
                MagicMock(status=CheckStatus.WARN),
            ]
        )
        assert r.passed is True
        assert r.warn_count == 1

    def test_fail_blocks(self):
        r = PreflightResult(
            checks=[
                MagicMock(status=CheckStatus.PASS),
                MagicMock(status=CheckStatus.FAIL),
            ]
        )
        assert r.passed is False
        assert r.fail_count == 1


# ---------------------------------------------------------------------------
# Preflight connection checks
# ---------------------------------------------------------------------------


class TestPreflightConnection:
    @pytest.mark.asyncio
    async def test_connection_pass(self):
        from novatrade.validation.ftmo_preflight import check_connection

        adapter = AsyncMock()
        adapter.connect.return_value = HealthStatus(
            state=HealthState.OK,
            connected=True,
            latency_ms=100.0,
        )
        adapter.get_account.return_value = AccountState(
            balance=10000.0,
            equity=10000.0,
            currency="USD",
            broker="FTMO",
            server="FTMO-Demo",
        )
        adapter.get_positions.return_value = []
        adapter.get_symbol_price.return_value = SymbolPrice(symbol="EURUSD", bid=1.1000, ask=1.1002)

        cfg = NovaTradeCfg(symbols=["EURUSD"])
        checks = await check_connection(cfg, adapter)
        statuses = {c.name: c.status for c in checks}

        assert statuses["adapter_connect"] == CheckStatus.PASS
        assert statuses["account_readable"] == CheckStatus.PASS
        assert statuses["positions_readable"] == CheckStatus.PASS
        assert statuses["symbol_EURUSD"] == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_connection_fail_stops_early(self):
        from novatrade.validation.ftmo_preflight import check_connection

        adapter = AsyncMock()
        adapter.connect.return_value = HealthStatus(
            state=HealthState.DOWN,
            connected=False,
            message="timeout",
        )

        cfg = NovaTradeCfg(symbols=["EURUSD"])
        checks = await check_connection(cfg, adapter)

        assert len(checks) == 1
        assert checks[0].status == CheckStatus.FAIL
        assert checks[0].name == "adapter_connect"

    @pytest.mark.asyncio
    async def test_symbol_resolution_with_ftmo(self):
        from novatrade.validation.ftmo_preflight import check_connection

        adapter = AsyncMock()
        adapter.connect.return_value = HealthStatus(state=HealthState.OK, connected=True)
        adapter.get_account.return_value = AccountState(balance=10000.0, equity=10000.0)
        adapter.get_positions.return_value = []
        adapter.get_symbol_price.return_value = SymbolPrice(symbol="EURUSD.ftmo", bid=1.1000, ask=1.1002)

        cfg = NovaTradeCfg(
            symbols=["EURUSD"],
            ftmo=FtmoProfile(enabled=True, symbol_suffix=".ftmo"),
        )
        checks = await check_connection(cfg, adapter)

        sym_check = next(c for c in checks if c.name == "symbol_EURUSD")
        assert sym_check.status == CheckStatus.PASS
        assert "EURUSD→EURUSD.ftmo" in sym_check.detail

        # Verify adapter was called with mapped symbol
        adapter.get_symbol_price.assert_called_with("EURUSD.ftmo")


# ---------------------------------------------------------------------------
# Evidence campaign tagging
# ---------------------------------------------------------------------------


class TestEvidenceCampaign:
    def test_campaign_tag_injected(self, tmp_path):
        path = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(path=path, campaign="FTMO-free_trial")
        recorder.record_execution(
            ExecutionResult(
                outcome=ExecutionOutcome.DENIED,
                error="",
            )
        )

        records = recorder.load()
        assert len(records) == 1
        assert records[0].data["campaign"] == "FTMO-free_trial"

    def test_no_campaign_tag_when_empty(self, tmp_path):
        path = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(path=path)
        recorder.record_execution(
            ExecutionResult(
                outcome=ExecutionOutcome.DENIED,
                error="",
            )
        )

        records = recorder.load()
        assert len(records) == 1
        assert "campaign" not in records[0].data

    def test_campaign_on_all_record_types(self, tmp_path):
        from novatrade.models import (
            HealthSnapshot,
            HealthState,
            HealthStatus,
            ReconciliationResult,
            RiskDecision,
            RiskVerdict,
        )

        path = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(path=path, campaign="test-campaign")

        recorder.record_health(
            HealthSnapshot(
                adapter_health=HealthStatus(state=HealthState.OK, connected=True),
            )
        )
        recorder.record_reconciliation(ReconciliationResult(ok=True))
        recorder.record_risk_decision(RiskDecision(verdict=RiskVerdict.ALLOW), symbol="EURUSD")
        recorder.record_error("test error")

        records = recorder.load()
        assert len(records) == 4
        for rec in records:
            assert rec.data["campaign"] == "test-campaign"


# ---------------------------------------------------------------------------
# Format preflight
# ---------------------------------------------------------------------------


class TestFormatPreflight:
    def test_pass_format(self):
        from novatrade.validation.ftmo_preflight import PreflightCheck

        result = PreflightResult(
            checks=[
                PreflightCheck("config_valid", CheckStatus.PASS, "ok"),
                PreflightCheck("ftmo_enabled", CheckStatus.PASS, "FTMO"),
            ],
            elapsed_ms=50.0,
        )
        text = format_preflight(result, campaign="test")
        assert "PREFLIGHT PASSED" in text
        assert "[test]" in text
        assert "[OK]" in text

    def test_fail_format(self):
        from novatrade.validation.ftmo_preflight import PreflightCheck

        result = PreflightResult(
            checks=[
                PreflightCheck("mode_demo", CheckStatus.FAIL, "wrong mode"),
            ],
            elapsed_ms=10.0,
        )
        text = format_preflight(result)
        assert "PREFLIGHT FAILED" in text
        assert "[XX]" in text

    def test_warn_format(self):
        from novatrade.validation.ftmo_preflight import PreflightCheck

        result = PreflightResult(
            checks=[
                PreflightCheck("dry_run_safe", CheckStatus.WARN, "dry_run=false"),
            ],
            elapsed_ms=10.0,
        )
        text = format_preflight(result)
        assert "PREFLIGHT PASSED" in text
        assert "[!!]" in text


# ---------------------------------------------------------------------------
# Report campaign label
# ---------------------------------------------------------------------------


class TestReportCampaignLabel:
    def test_campaign_in_report_header(self):
        from novatrade.models import ValidationReport
        from novatrade.validation.reporting import format_report

        report = ValidationReport(total_records=1)
        text = format_report(report, campaign="FTMO-free_trial")
        assert "[FTMO-free_trial]" in text

    def test_no_campaign_no_bracket(self):
        from novatrade.models import ValidationReport
        from novatrade.validation.reporting import format_report

        report = ValidationReport(total_records=1)
        text = format_report(report)
        assert "[" not in text
