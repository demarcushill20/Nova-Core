"""Tests for novatrade.execution.demo_executor — end-to-end execution path."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.execution.demo_executor import DemoExecutor
from novatrade.models import (
    AccountMode,
    AccountState,
    ExecutionOutcome,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    RiskVerdict,
    SymbolPrice,
)
from novatrade.risk.pre_trade_gate import PreTradeGate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    risk_kw = overrides.pop("risk", {})
    risk = RiskConfig(**{**{"require_stop_loss": True}, **risk_kw})
    defaults = {
        "dry_run": False,
        "symbols": ["EURUSD", "GBPUSD"],
        "risk": risk,
    }
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _order(**kw) -> OrderRequest:
    defaults = {
        "symbol": "EURUSD",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "volume": 0.1,
        "stop_loss": 1.0900,
    }
    defaults.update(kw)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


def _mock_adapter(
    *,
    account: AccountState | None = None,
    positions: list[Position] | None = None,
    health: HealthStatus | None = None,
    price: SymbolPrice | None = None,
    order_result: OrderResult | None = None,
) -> AsyncMock:
    adapter = AsyncMock()
    adapter.health_check = AsyncMock(
        return_value=health
        or HealthStatus(
            state=HealthState.OK,
            connected=True,
        )
    )
    adapter.get_account = AsyncMock(
        return_value=account
        or AccountState(
            balance=10000.0,
            equity=10000.0,
            mode=AccountMode.DEMO,
        )
    )
    adapter.get_positions = AsyncMock(return_value=positions if positions is not None else [])
    adapter.get_symbol_price = AsyncMock(
        return_value=price
        or SymbolPrice(
            symbol="EURUSD",
            bid=1.10000,
            ask=1.10020,
        )
    )
    adapter.place_order = AsyncMock(
        return_value=order_result
        or OrderResult(
            ok=True,
            order_id="ORD-001",
            status=OrderStatus.FILLED,
        )
    )
    return adapter


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Happy path — full execution
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_order_fills_successfully(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.ok
        assert result.outcome == ExecutionOutcome.FILLED
        assert result.order_result is not None
        assert result.order_result.order_id == "ORD-001"
        assert result.risk_decision is not None
        assert result.risk_decision.verdict == RiskVerdict.ALLOW
        assert result.elapsed_ms > 0
        adapter.place_order.assert_called_once()

    def test_gate_sees_fresh_account_and_positions(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            positions=[
                Position(
                    position_id="P1",
                    symbol="GBPUSD",
                    side=OrderSide.SELL,
                    volume=0.1,
                    open_price=1.25,
                )
            ],
        )
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.ok
        adapter.get_account.assert_called_once()
        adapter.get_positions.assert_called_once()

    def test_trade_recorded_after_fill(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        gate = PreTradeGate(cfg)
        executor = DemoExecutor(cfg, adapter, gate)

        result = _run(executor.execute(_order()))
        assert result.ok

        # Gate should have the trade recorded — next same-symbol+side should cooldown
        assert len(gate._trade_log) == 1
        assert gate._trade_log[0][1] == "EURUSD"
        assert gate._trade_log[0][2] == "BUY"


# ---------------------------------------------------------------------------
# Gate denial — adapter not called
# ---------------------------------------------------------------------------


class TestGateDenial:
    def test_dry_run_denies(self):
        cfg = _cfg(dry_run=True)
        adapter = _mock_adapter()
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.denied
        assert result.outcome == ExecutionOutcome.DENIED
        assert result.risk_decision is not None
        assert result.risk_decision.denied
        assert result.order_result is None
        adapter.place_order.assert_not_called()

    def test_disallowed_symbol_denies(self):
        cfg = _cfg(symbols=["GBPUSD"])
        adapter = _mock_adapter()
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order(symbol="EURUSD")))

        assert result.denied
        adapter.place_order.assert_not_called()
        assert "symbol_allowed" in result.risk_decision.rule

    def test_missing_stop_loss_denies(self):
        cfg = _cfg(risk={"require_stop_loss": True})
        adapter = _mock_adapter()
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order(stop_loss=None)))

        assert result.denied
        adapter.place_order.assert_not_called()

    def test_max_positions_denies(self):
        cfg = _cfg(risk={"max_positions": 1})
        adapter = _mock_adapter(
            positions=[
                Position(position_id="P1", symbol="GBPUSD", side=OrderSide.SELL, volume=0.1, open_price=1.25),
            ]
        )
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.denied
        adapter.place_order.assert_not_called()

    def test_challenge_mode_denies_in_mvp(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            account=AccountState(
                balance=10000,
                equity=10000,
                mode=AccountMode.CHALLENGE,
            )
        )
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.denied
        adapter.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Adapter error — no retry
# ---------------------------------------------------------------------------


class TestAdapterError:
    def test_adapter_returns_failure(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            order_result=OrderResult(
                ok=False,
                error="insufficient margin",
                status=OrderStatus.REJECTED,
            )
        )
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert not result.ok
        assert result.outcome == ExecutionOutcome.ADAPTER_ERROR
        assert result.error == "insufficient margin"
        assert result.order_result is not None
        assert result.risk_decision.verdict == RiskVerdict.ALLOW
        adapter.place_order.assert_called_once()

    def test_adapter_failure_does_not_record_trade(self):
        cfg = _cfg()
        adapter = _mock_adapter(order_result=OrderResult(ok=False, error="fail"))
        gate = PreTradeGate(cfg)
        executor = DemoExecutor(cfg, adapter, gate)

        result = _run(executor.execute(_order()))

        assert result.outcome == ExecutionOutcome.ADAPTER_ERROR
        assert len(gate._trade_log) == 0  # not recorded


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_adapter_exception_caught(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        adapter.get_account = AsyncMock(side_effect=ConnectionError("lost connection"))
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.outcome == ExecutionOutcome.EXCEPTION
        assert "lost connection" in result.error
        assert result.elapsed_ms > 0

    def test_place_order_exception_caught(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        adapter.place_order = AsyncMock(side_effect=RuntimeError("SDK crash"))
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.outcome == ExecutionOutcome.EXCEPTION
        assert "SDK crash" in result.error


# ---------------------------------------------------------------------------
# Optional context — health and price
# ---------------------------------------------------------------------------


class TestOptionalContext:
    def test_pre_fetched_health_used(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        pre_health = HealthStatus(state=HealthState.OK, connected=True)
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order(), health=pre_health))

        assert result.ok
        # Adapter health_check should not be called since we passed it in
        adapter.health_check.assert_not_called()

    def test_pre_fetched_price_used(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        pre_price = SymbolPrice(symbol="EURUSD", bid=1.1, ask=1.10015)
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order(), price=pre_price))

        assert result.ok
        adapter.get_symbol_price.assert_not_called()

    def test_price_fetch_failure_non_fatal(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        adapter.get_symbol_price = AsyncMock(side_effect=Exception("no price"))
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        # Should still succeed — price is optional for spread check
        assert result.ok

    def test_health_down_denies(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            health=HealthStatus(
                state=HealthState.DOWN,
                message="timeout",
            )
        )
        executor = DemoExecutor(cfg, adapter)

        result = _run(executor.execute(_order()))

        assert result.denied
        adapter.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Cooldown integration
# ---------------------------------------------------------------------------


class TestCooldownIntegration:
    def test_second_order_same_symbol_side_denied(self):
        cfg = _cfg(risk={"cooldown_seconds": 300})
        adapter = _mock_adapter()
        gate = PreTradeGate(cfg)
        executor = DemoExecutor(cfg, adapter, gate)

        # First order succeeds
        r1 = _run(executor.execute(_order()))
        assert r1.ok

        # Second identical order denied by cooldown
        r2 = _run(executor.execute(_order()))
        assert r2.denied
        failed_names = {c.name for c in r2.risk_decision.failed_checks}
        assert "cooldown" in failed_names

    def test_different_symbol_not_blocked(self):
        cfg = _cfg(risk={"cooldown_seconds": 300})
        adapter = _mock_adapter()
        gate = PreTradeGate(cfg)
        executor = DemoExecutor(cfg, adapter, gate)

        r1 = _run(executor.execute(_order(symbol="EURUSD")))
        assert r1.ok

        r2 = _run(executor.execute(_order(symbol="GBPUSD")))
        assert r2.ok


# ---------------------------------------------------------------------------
# ExecutionResult model
# ---------------------------------------------------------------------------


class TestExecutionResultModel:
    def test_filled_properties(self):
        from novatrade.models import ExecutionResult

        r = ExecutionResult(outcome=ExecutionOutcome.FILLED)
        assert r.ok
        assert not r.denied

    def test_denied_properties(self):
        from novatrade.models import ExecutionResult

        r = ExecutionResult(outcome=ExecutionOutcome.DENIED)
        assert not r.ok
        assert r.denied

    def test_error_properties(self):
        from novatrade.models import ExecutionResult

        r = ExecutionResult(outcome=ExecutionOutcome.ADAPTER_ERROR, error="fail")
        assert not r.ok
        assert not r.denied
        assert r.error == "fail"
