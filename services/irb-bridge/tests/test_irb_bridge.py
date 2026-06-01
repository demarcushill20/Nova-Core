# Tests for the IRB bridge sizing + reconcile state machine.

# Pure functions must import without MetaApi/env side effects.
from irb_bridge import SizingCfg, _round_step, risk_based_lot

CFG = SizingCfg(risk_pct=1.0, contract_size=100_000, min_lot=0.01, max_lot=50.0, lot_step=0.01, base_lot=0.10)


def test_round_step():
    assert _round_step(35.714, 0.01) == 35.71
    assert _round_step(0.004, 0.01) == 0.0


def test_risk_based_lot_one_percent_eurusd():
    # equity 100k, 1% = $1000 risk; stop 28 points (2.8 pips) = 0.00028
    lot = risk_based_lot(entry=1.08500, stop=1.08472, equity=100_000, cfg=CFG)
    # raw = 1000 / (100000 * 0.00028) = 35.714 -> 35.71
    assert lot == 35.71


def test_risk_based_lot_clamped_to_max():
    cfg = SizingCfg(1.0, 100_000, 0.01, 5.0, 0.01, 0.10)
    assert risk_based_lot(1.08500, 1.08472, 100_000, cfg) == 5.0


def test_risk_based_lot_floors_to_min():
    # tiny equity ($100 -> $1 risk) over a wide stop -> sub-min raw lot -> min_lot floor
    assert risk_based_lot(1.08500, 1.00000, 100, CFG) == 0.01


def test_risk_based_lot_fallback_on_bad_inputs():
    assert risk_based_lot(None, 1.0, 100_000, CFG) == 0.10  # missing entry
    assert risk_based_lot(1.085, 1.085, 100_000, CFG) == 0.10  # entry == stop
    assert risk_based_lot(1.085, 1.084, None, CFG) == 0.10  # equity unavailable
    assert risk_based_lot(1.085, 1.084, 0.0, CFG) == 0.10  # zero equity
