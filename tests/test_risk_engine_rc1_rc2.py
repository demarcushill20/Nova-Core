"""Tests for RC1 (halt state persistence) and RC2 (zero/negative equity) fixes.

RC1: Halt state is now persisted across restarts via STATE/novatrade_risk_state.json.
     initialize() reads the file and restores _halted / _halt_reason.

RC2: Zero or negative equity now triggers an immediate emergency halt instead
     of silently skipping all drawdown checks.
"""

import json
from pathlib import Path

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderRequest,
    OrderSide,
    OrderType,
    RiskVerdict,
)
from novatrade.risk.risk_engine import RiskEngine

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
    )
    defaults = dict(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=risk,
        dry_run=False,
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _account(equity: float = 100_000, balance: float = 100_000) -> AccountState:
    return AccountState(
        balance=balance,
        equity=equity,
        mode=AccountMode.DEMO,
    )


def _order(
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.10,
    price: float = 1.1000,
    stop_loss: float = 1.0950,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=OrderType.STOP,
        volume=volume,
        price=price,
        stop_loss=stop_loss,
    )


def _write_state_file(path: Path, data: dict) -> None:
    """Write a JSON risk state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# RC1: Halt state persistence across restarts
# ---------------------------------------------------------------------------


class TestRC1HaltPersistence:
    """Verify that halt state survives process restarts."""

    def test_initialize_restores_halt_from_disk(self, tmp_path: Path):
        """If persisted state has halted=true, engine starts halted."""
        state_file = tmp_path / "risk_state.json"
        _write_state_file(
            state_file,
            {
                "halted": True,
                "halt_reason": "FTMO daily drawdown limit reached",
                "equity_drawdown_pct": 6.5,
                "daily_drawdown_pct": 5.2,
            },
        )

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000), state_file=state_file)

        assert engine.halted is True
        assert "FTMO daily drawdown" in engine.halt_reason

        # Trades should be blocked at layer 0
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.HALT
        assert decision.policy_layer == 0

    def test_initialize_no_state_file_starts_clean(self, tmp_path: Path):
        """No state file on disk => engine starts normally (not halted)."""
        state_file = tmp_path / "nonexistent_risk_state.json"

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000), state_file=state_file)

        assert engine.halted is False
        assert engine.halt_reason == ""

    def test_initialize_malformed_state_file_starts_clean(self, tmp_path: Path):
        """Malformed JSON on disk => engine starts normally with a warning."""
        state_file = tmp_path / "risk_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("NOT VALID JSON {{{", encoding="utf-8")

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000), state_file=state_file)

        assert engine.halted is False
        assert engine.halt_reason == ""

    def test_halt_reason_persisted_in_risk_state(self, tmp_path: Path):
        """write_risk_state() includes halt_reason in the JSON payload."""
        state_file = tmp_path / "risk_state.json"

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        engine._halt("FTMO total drawdown limit reached")
        engine.write_risk_state(state_file=state_file)

        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["halted"] is True
        assert data["halt_reason"] == "FTMO total drawdown limit reached"

    def test_resume_clears_halt_even_after_restart(self, tmp_path: Path):
        """Halt -> write -> new engine -> initialize (restores halt) -> resume() -> unhalted."""
        state_file = tmp_path / "risk_state.json"

        # Original engine halts and writes state
        engine1 = RiskEngine(_cfg())
        engine1.initialize(_account(equity=100_000))
        engine1._halt("daily drawdown breach")
        engine1.write_risk_state(state_file=state_file)
        assert engine1.halted is True

        # Simulate restart: new engine reads persisted state
        engine2 = RiskEngine(_cfg())
        engine2.initialize(_account(equity=100_000), state_file=state_file)
        assert engine2.halted is True
        assert "daily drawdown breach" in engine2.halt_reason

        # Operator resumes
        engine2.resume()
        assert engine2.halted is False
        assert engine2.halt_reason == ""

        # Trades should be allowed again
        decision = engine2.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_initialize_halted_false_on_disk_starts_clean(self, tmp_path: Path):
        """If persisted state has halted=false, engine starts normally."""
        state_file = tmp_path / "risk_state.json"
        _write_state_file(
            state_file,
            {
                "halted": False,
                "halt_reason": "",
                "equity_drawdown_pct": 2.0,
            },
        )

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000), state_file=state_file)

        assert engine.halted is False

    def test_initialize_missing_halt_reason_uses_default(self, tmp_path: Path):
        """If state file has halted=true but no halt_reason, a default is used."""
        state_file = tmp_path / "risk_state.json"
        _write_state_file(state_file, {"halted": True})

        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000), state_file=state_file)

        assert engine.halted is True
        assert engine.halt_reason != ""  # should have a default reason
