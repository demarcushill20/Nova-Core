import pytest
from discord_mirror.sizing import PositionPlan, SymbolSpec, allocate_positions

GOLD = SymbolSpec(
    symbol="XAUUSD",
    contract_size=100.0,
    tick_size=0.01,
    tick_value=1.0,
    min_lot=0.01,
    lot_step=0.01,
)


def test_single_tp_uses_full_risk():
    # SL distance 20 → 2000 ticks → risk_per_lot $2000.
    # 1% of $10k = $100 → lot 0.05.
    plans = allocate_positions(
        balance=10_000.0,
        account_risk_pct=0.01,
        direction="BUY",
        entry=4500.0,
        sl=4480.0,
        tps=[4520.0],
        symbol=GOLD,
    )
    assert len(plans) == 1
    assert plans[0].lot == pytest.approx(0.05, abs=0.001)
    assert plans[0].tp == 4520.0
    assert plans[0].sl == 4480.0


def test_three_tps_split():
    plans = allocate_positions(
        balance=10_000.0,
        account_risk_pct=0.01,
        direction="BUY",
        entry=4500.0,
        sl=4480.0,
        tps=[4520.0, 4530.0, 4540.0],
        symbol=GOLD,
    )
    assert len(plans) == 3
    # Per-leg ideal 0.05/3 ≈ 0.0167 → rounds DOWN to 0.01 (lot_step).
    total = sum(p.lot for p in plans)
    assert total <= 0.051  # combined risk ≤ 1% (after rounding down)
    assert all(p.lot == 0.01 for p in plans)
    assert [p.tp for p in plans] == [4520.0, 4530.0, 4540.0]


def test_below_min_lot_returns_empty():
    # 1% of $1000 = $10 → ideal 0.005 → < min 0.01 → skip.
    plans = allocate_positions(
        balance=1_000.0,
        account_risk_pct=0.01,
        direction="BUY",
        entry=4500.0,
        sl=4480.0,
        tps=[4520.0, 4530.0],
        symbol=GOLD,
    )
    assert plans == []


def test_sell_direction():
    plans = allocate_positions(
        balance=10_000.0,
        account_risk_pct=0.01,
        direction="SELL",
        entry=4500.0,
        sl=4520.0,
        tps=[4480.0],
        symbol=GOLD,
    )
    assert len(plans) == 1
    assert plans[0].lot == pytest.approx(0.05, abs=0.001)
    assert plans[0].direction == "SELL"


def test_zero_sl_distance_returns_empty():
    plans = allocate_positions(
        balance=10_000.0,
        account_risk_pct=0.01,
        direction="BUY",
        entry=4500.0,
        sl=4500.0,
        tps=[4520.0],
        symbol=GOLD,
    )
    assert plans == []


def test_position_plan_is_immutable():
    p = PositionPlan(direction="BUY", symbol="XAUUSD", entry=4500.0, sl=4480.0, tp=4520.0, lot=0.05)
    with pytest.raises((AttributeError, TypeError)):
        p.lot = 0.10  # type: ignore[misc]


def test_registry_has_xauusd():
    from discord_mirror.sizing import get_symbol_spec

    spec = get_symbol_spec("XAUUSD")
    assert spec.contract_size == 100.0
    assert spec.tick_size == 0.01


def test_registry_has_eurusd():
    from discord_mirror.sizing import get_symbol_spec

    spec = get_symbol_spec("EURUSD")
    assert spec.contract_size == 100_000.0
    assert spec.tick_value == 10.0


def test_registry_unknown_raises():
    from discord_mirror.sizing import get_symbol_spec

    with pytest.raises(KeyError):
        get_symbol_spec("FAKEPAIR")
