"""Tests for equity drawdown kill switch — Phase 6B Step 6.16.

Covers:
- check_equity_kill_switch() with breached / safe / missing / malformed state
- is_novatrade_task() classification
- RiskEngine.should_kill_switch() integration
- RiskEngine.write_risk_state() produces valid JSON
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nova_kill_switch import check_equity_kill_switch, is_novatrade_task
from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountState
from novatrade.risk.risk_engine import RiskEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_engine(
    initial_equity: float = 100_000.0,
    daily_dd_pct: float = 5.0,
    total_dd_pct: float = 10.0,
) -> RiskEngine:
    """Create a RiskEngine with known limits and initialize it."""
    cfg = NovaTradeCfg(
        risk=RiskConfig(
            max_daily_drawdown_pct=daily_dd_pct,
            max_total_drawdown_pct=total_dd_pct,
        )
    )
    engine = RiskEngine(cfg)
    engine.initialize(AccountState(balance=initial_equity, equity=initial_equity))
    return engine


# ---------------------------------------------------------------------------
# check_equity_kill_switch
# ---------------------------------------------------------------------------


class TestCheckEquityKillSwitch:
    """Tests for the file-based equity kill switch reader."""

    def test_breached_returns_true(self, tmp_path: Path) -> None:
        sf = tmp_path / "risk_state.json"
        _write_state(sf, {"equity_drawdown_pct": 12.5, "max_allowed_drawdown_pct": 10.0, "breached": True})
        assert check_equity_kill_switch(sf) is True

    def test_not_breached_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "risk_state.json"
        _write_state(sf, {"equity_drawdown_pct": 5.2, "max_allowed_drawdown_pct": 10.0, "breached": False})
        assert check_equity_kill_switch(sf) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "nonexistent.json"
        assert check_equity_kill_switch(sf) is False

    def test_malformed_json_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "bad.json"
        sf.write_text("{not valid json!!!", encoding="utf-8")
        assert check_equity_kill_switch(sf) is False

    def test_empty_file_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "empty.json"
        sf.write_text("", encoding="utf-8")
        assert check_equity_kill_switch(sf) is False

    def test_non_dict_json_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "list.json"
        sf.write_text("[1,2,3]", encoding="utf-8")
        assert check_equity_kill_switch(sf) is False

    def test_missing_breached_key_returns_false(self, tmp_path: Path) -> None:
        sf = tmp_path / "no_key.json"
        _write_state(sf, {"equity_drawdown_pct": 15.0})
        assert check_equity_kill_switch(sf) is False

    def test_exactly_at_limit_breached(self, tmp_path: Path) -> None:
        sf = tmp_path / "risk_state.json"
        _write_state(sf, {"equity_drawdown_pct": 10.0, "max_allowed_drawdown_pct": 10.0, "breached": True})
        assert check_equity_kill_switch(sf) is True

    def test_zero_drawdown_not_breached(self, tmp_path: Path) -> None:
        sf = tmp_path / "risk_state.json"
        _write_state(sf, {"equity_drawdown_pct": 0.0, "max_allowed_drawdown_pct": 10.0, "breached": False})
        assert check_equity_kill_switch(sf) is False


# ---------------------------------------------------------------------------
# is_novatrade_task
# ---------------------------------------------------------------------------


class TestIsNovatradeTask:
    """Tests for NovaTrade task classification."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("novatrade_webhook_handler", True),
            ("ftmo_drawdown_check", True),
            ("deploy_trading_bot", True),
            ("backtest_irb_strategy", True),
            ("forex_signal_processor", True),
            ("mt5_bridge_setup", True),
            ("update_readme", False),
            ("heartbeat_check", False),
            ("memory_maintenance", False),
            ("install_packages", False),
        ],
    )
    def test_task_name_classification(self, name: str, expected: bool) -> None:
        assert is_novatrade_task(name) is expected

    def test_content_matches(self) -> None:
        assert is_novatrade_task("generic_task", "This task deploys the NovaTrade system") is True

    def test_case_insensitive(self) -> None:
        assert is_novatrade_task("FOREX_SETUP") is True
        assert is_novatrade_task("FtMo_Check") is True

    def test_empty_inputs(self) -> None:
        assert is_novatrade_task("") is False
        assert is_novatrade_task("", "") is False


# ---------------------------------------------------------------------------
# RiskEngine.should_kill_switch
# ---------------------------------------------------------------------------


class TestRiskEngineShouldKillSwitch:
    """Tests for the RiskEngine.should_kill_switch() method."""

    def test_no_drawdown_returns_false(self) -> None:
        engine = _make_engine(100_000.0)
        assert engine.should_kill_switch() is False

    def test_total_drawdown_breach_returns_true(self) -> None:
        engine = _make_engine(100_000.0, total_dd_pct=10.0)
        # Simulate equity drop: update drawdown tracking
        engine._total_dd.update(100_000.0)  # set peak
        engine._total_dd.update(89_000.0)  # 11% DD from reference
        assert engine.should_kill_switch() is True

    def test_daily_drawdown_breach_returns_true(self) -> None:
        engine = _make_engine(100_000.0, daily_dd_pct=5.0)
        engine._daily_dd.update(100_000.0)
        engine._daily_dd.update(94_000.0)  # 6% DD from reference
        assert engine.should_kill_switch() is True

    def test_below_limit_returns_false(self) -> None:
        engine = _make_engine(100_000.0, total_dd_pct=10.0)
        engine._total_dd.update(100_000.0)
        engine._total_dd.update(95_000.0)  # 5% — below 10% limit
        assert engine.should_kill_switch() is False

    def test_exactly_at_limit_returns_true(self) -> None:
        engine = _make_engine(100_000.0, total_dd_pct=10.0)
        engine._total_dd.update(100_000.0)
        engine._total_dd.update(90_000.0)  # exactly 10%
        assert engine.should_kill_switch() is True

    def test_negative_drawdown_returns_false(self) -> None:
        """Equity above reference → no drawdown."""
        engine = _make_engine(100_000.0)
        engine._total_dd.update(105_000.0)  # equity went up
        assert engine.should_kill_switch() is False


# ---------------------------------------------------------------------------
# RiskEngine.write_risk_state
# ---------------------------------------------------------------------------


class TestWriteRiskState:
    """Tests for the RiskEngine.write_risk_state() method."""

    def test_produces_valid_json(self, tmp_path: Path) -> None:
        engine = _make_engine(100_000.0)
        sf = tmp_path / "risk_state.json"
        engine.write_risk_state(sf)

        data = json.loads(sf.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "equity_drawdown_pct" in data
        assert "max_allowed_drawdown_pct" in data
        assert "breached" in data
        assert "last_updated" in data
        assert "daily_drawdown_pct" in data
        assert "halted" in data

    def test_breached_field_reflects_state(self, tmp_path: Path) -> None:
        engine = _make_engine(100_000.0, total_dd_pct=10.0)
        sf = tmp_path / "risk_state.json"

        # No drawdown → not breached
        engine.write_risk_state(sf)
        data = json.loads(sf.read_text(encoding="utf-8"))
        assert data["breached"] is False

        # Simulate breach
        engine._total_dd.update(100_000.0)
        engine._total_dd.update(89_000.0)  # 11% DD
        engine.write_risk_state(sf)
        data = json.loads(sf.read_text(encoding="utf-8"))
        assert data["breached"] is True
        assert data["equity_drawdown_pct"] >= 10.0

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        sf = tmp_path / "nested" / "dir" / "state.json"
        engine = _make_engine()
        engine.write_risk_state(sf)
        assert sf.exists()

    def test_roundtrip_with_check_equity_kill_switch(self, tmp_path: Path) -> None:
        """write_risk_state → check_equity_kill_switch roundtrip."""
        engine = _make_engine(100_000.0, total_dd_pct=10.0)
        sf = tmp_path / "state.json"

        # Safe state
        engine.write_risk_state(sf)
        assert check_equity_kill_switch(sf) is False

        # Breach state
        engine._total_dd.update(100_000.0)
        engine._total_dd.update(85_000.0)
        engine.write_risk_state(sf)
        assert check_equity_kill_switch(sf) is True
