"""Tests for novatrade.config — configuration loading and validation."""

import os
import tempfile
from pathlib import Path

from novatrade.config import MetaApiConfig, NovaTradeCfg, RiskConfig, _load_env_file
from novatrade.models import AccountMode

# ---------------------------------------------------------------------------
# MetaApiConfig
# ---------------------------------------------------------------------------


class TestMetaApiConfig:
    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("METAAPI_TOKEN", raising=False)
        monkeypatch.delenv("METAAPI_ACCOUNT_ID", raising=False)
        cfg = MetaApiConfig.from_env()
        assert cfg.token == ""
        assert cfg.account_id == ""
        assert cfg.application == "NovaTrade"

    def test_from_env_populated(self, monkeypatch):
        monkeypatch.setenv("METAAPI_TOKEN", "test-token-xyz")
        monkeypatch.setenv("METAAPI_ACCOUNT_ID", "acct-42")
        cfg = MetaApiConfig.from_env()
        assert cfg.token == "test-token-xyz"
        assert cfg.account_id == "acct-42"

    def test_validate_missing_token(self):
        cfg = MetaApiConfig(token="", account_id="x")
        errors = cfg.validate()
        assert any("TOKEN" in e for e in errors)

    def test_validate_missing_account_id(self):
        cfg = MetaApiConfig(token="t", account_id="")
        errors = cfg.validate()
        assert any("ACCOUNT_ID" in e for e in errors)

    def test_validate_both_present(self):
        cfg = MetaApiConfig(token="t", account_id="a")
        assert cfg.validate() == []


# ---------------------------------------------------------------------------
# RiskConfig
# ---------------------------------------------------------------------------


class TestRiskConfig:
    def test_defaults_valid(self):
        assert RiskConfig().validate() == []

    def test_invalid_drawdown(self):
        cfg = RiskConfig(max_daily_drawdown_pct=0)
        assert len(cfg.validate()) > 0

    def test_invalid_positions(self):
        cfg = RiskConfig(max_positions=0)
        errors = cfg.validate()
        assert any("max_positions" in e for e in errors)


# ---------------------------------------------------------------------------
# NovaTradeCfg
# ---------------------------------------------------------------------------


class TestNovaTradeCfg:
    def test_defaults(self):
        cfg = NovaTradeCfg()
        assert cfg.mode == AccountMode.DEMO
        assert cfg.dry_run is True
        assert "EURUSD" in cfg.symbols

    def test_load_defaults(self, monkeypatch):
        # clear any env vars that might interfere
        for key in (
            "NOVATRADE_MODE",
            "NOVATRADE_SYMBOLS",
            "NOVATRADE_TIMEFRAMES",
            "NOVATRADE_PROVIDER",
            "NOVATRADE_DRY_RUN",
            "METAAPI_TOKEN",
            "METAAPI_ACCOUNT_ID",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.mode == AccountMode.DEMO
        assert cfg.symbols == ["EURUSD"]
        assert cfg.dry_run is True

    def test_load_with_env(self, monkeypatch):
        monkeypatch.setenv("NOVATRADE_MODE", "CHALLENGE")
        monkeypatch.setenv("NOVATRADE_SYMBOLS", "EURUSD,GBPUSD,USDJPY")
        monkeypatch.setenv("NOVATRADE_DRY_RUN", "false")
        monkeypatch.setenv("METAAPI_TOKEN", "tok")
        monkeypatch.setenv("METAAPI_ACCOUNT_ID", "acc")

        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.mode == AccountMode.CHALLENGE
        assert cfg.symbols == ["EURUSD", "GBPUSD", "USDJPY"]
        assert cfg.dry_run is False

    def test_load_invalid_mode_falls_back(self, monkeypatch):
        monkeypatch.setenv("NOVATRADE_MODE", "INVALID_MODE")
        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.mode == AccountMode.DEMO

    def test_validate_no_symbols(self):
        cfg = NovaTradeCfg(symbols=[])
        errors = cfg.validate()
        assert any("symbol" in e for e in errors)

    def test_validate_no_timeframes(self):
        cfg = NovaTradeCfg(timeframes=[])
        errors = cfg.validate()
        assert any("timeframe" in e for e in errors)

    def test_validate_metaapi_checked(self):
        cfg = NovaTradeCfg(provider="metaapi")
        errors = cfg.validate()
        assert any("TOKEN" in e for e in errors)

    def test_validate_non_metaapi_skips_metaapi(self):
        cfg = NovaTradeCfg(provider="direct_mt5")
        errors = cfg.validate()
        assert not any("TOKEN" in e for e in errors)


# ---------------------------------------------------------------------------
# _load_env_file
# ---------------------------------------------------------------------------


class TestLoadEnvFile:
    def test_parses_simple_keyval(self, monkeypatch):
        monkeypatch.delenv("TEST_NOVATRADE_KEY", raising=False)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TEST_NOVATRADE_KEY=hello_world\n")
            f.write("# this is a comment\n")
            f.write("\n")
            f.write("TEST_NOVATRADE_QUOTED='quoted_value'\n")
            f.flush()
            _load_env_file(Path(f.name))

        assert os.environ.get("TEST_NOVATRADE_KEY") == "hello_world"
        assert os.environ.get("TEST_NOVATRADE_QUOTED") == "quoted_value"

        # cleanup
        os.unlink(f.name)
        os.environ.pop("TEST_NOVATRADE_KEY", None)
        os.environ.pop("TEST_NOVATRADE_QUOTED", None)

    def test_does_not_override_existing(self, monkeypatch):
        monkeypatch.setenv("TEST_NOVATRADE_EXISTING", "original")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TEST_NOVATRADE_EXISTING=overridden\n")
            f.flush()
            _load_env_file(Path(f.name))

        assert os.environ["TEST_NOVATRADE_EXISTING"] == "original"
        os.unlink(f.name)
