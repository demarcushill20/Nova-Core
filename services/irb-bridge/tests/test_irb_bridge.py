# Tests for the IRB bridge sizing + reconcile state machine.
import asyncio

import pytest

# Pure functions must import without MetaApi/env side effects.
from irb_bridge import SizingCfg, _round_step, compute_desired_lot, reconcile, risk_based_lot

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


# --- State machine: compute_desired_lot -------------------------------------

EMPTY = {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}


def test_entry_sets_full_lot():
    lot, st = compute_desired_lot(
        EMPTY, position_size=10_000_000, comment="entry_long", entry=1.08500, stop=1.08472, equity=100_000, cfg=CFG
    )
    assert lot == 35.71
    assert st["side"] == 1
    assert st["entry_units"] == 10_000_000
    assert st["full_lot"] == 35.71
    assert st["last_fraction"] == 1.0


def test_partial_scales_to_half():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot, st2 = compute_desired_lot(
        st, position_size=5_000_000, comment="exit_long_tp1", entry=None, stop=None, equity=100_000, cfg=CFG
    )
    assert lot == _round_step(35.71 * 0.5, 0.01)  # 17.86
    assert st2["last_fraction"] == 0.5


def test_runner_to_flat():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 0.5}
    lot, st2 = compute_desired_lot(
        st, position_size=0, comment="exit_long_runner", entry=None, stop=None, equity=100_000, cfg=CFG
    )
    assert lot == 0.0
    assert st2["side"] == 0
    assert st2["last_fraction"] == 0.0


def test_runner_before_tp1_cannot_reopen():
    # Out-of-order: runner (pos=0) arrives first, then the stale TP1 (pos=5M).
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot1, st = compute_desired_lot(st, 0, "exit_long_runner", None, None, 100_000, CFG)
    assert lot1 == 0.0
    lot2, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot2 == 0.0  # monotonic guard: cannot grow back to 0.5


def test_duplicate_tp1_is_idempotent():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot1, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    lot2, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot1 == lot2 == _round_step(35.71 * 0.5, 0.01)


def test_new_entry_resets_state():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 0.5}
    lot, st2 = compute_desired_lot(st, -10_000_000, "entry_short", 1.08500, 1.08528, 100_000, CFG)
    assert lot < 0
    assert st2["side"] == -1
    assert st2["last_fraction"] == 1.0


def test_exit_with_no_open_trade_is_flat():
    lot, st = compute_desired_lot(EMPTY, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot == 0.0


# --- Reconcile serialization ------------------------------------------------


class FakeConn:
    """Minimal MetaApi RPC stand-in with realistic async settle delay."""

    def __init__(self):
        self.positions = []
        self._id = 0

    async def get_positions(self):
        await asyncio.sleep(0)
        return [dict(p) for p in self.positions]

    async def create_market_buy_order(self, sym, vol):
        await asyncio.sleep(0.01)  # settle delay where races happen
        self._id += 1
        self.positions.append({"id": str(self._id), "symbol": sym, "type": "POSITION_TYPE_BUY", "volume": vol})

    async def create_market_sell_order(self, sym, vol):
        await asyncio.sleep(0.01)
        self._id += 1
        self.positions.append({"id": str(self._id), "symbol": sym, "type": "POSITION_TYPE_SELL", "volume": vol})

    async def close_position(self, pid):
        await asyncio.sleep(0.01)
        self.positions = [p for p in self.positions if p["id"] != pid]

    async def close_position_partially(self, pid, vol):
        await asyncio.sleep(0.01)
        for p in self.positions:
            if p["id"] == pid:
                p["volume"] = round(p["volume"] - vol, 2)


def _net(conn):
    return sum(p["volume"] if p["type"] == "POSITION_TYPE_BUY" else -p["volume"] for p in conn.positions)


def test_concurrent_reconciles_serialize():
    async def run():
        conn = FakeConn()
        lock = asyncio.Lock()
        # entry to 0.10 then partial to 0.05 fire almost simultaneously;
        # last-submitted target (0.05) must win once serialized.
        await asyncio.gather(
            reconcile(conn, 0.10, "EURUSD", 0.005, lock),
            reconcile(conn, 0.05, "EURUSD", 0.005, lock),
        )
        return conn

    conn = asyncio.run(run())
    assert _net(conn) == pytest.approx(0.05, abs=0.005)


def test_sign_flip_closes_then_opens():
    async def run():
        conn = FakeConn()
        lock = asyncio.Lock()
        await reconcile(conn, 0.10, "EURUSD", 0.005, lock)
        await reconcile(conn, -0.10, "EURUSD", 0.005, lock)
        return conn

    conn = asyncio.run(run())
    assert _net(conn) == pytest.approx(-0.10, abs=0.005)


# --- Webhook end-to-end (DRY, no broker) ------------------------------------


def test_webhook_end_to_end_sizes_and_scales(tmp_path, monkeypatch):
    import json as _json

    import irb_bridge as ib

    monkeypatch.setattr(ib, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ib, "SECRET", "sek")
    monkeypatch.setattr(ib, "broker", None)  # no real broker; equity -> None -> base_lot
    monkeypatch.setattr(ib, "SIZING", ib.SizingCfg(1.0, 100_000, 0.01, 50.0, 0.01, 0.10))
    client = ib.app.test_client()

    def post(body):
        return client.post("/irb/webhook", data=_json.dumps(body), content_type="application/json")

    # entry with no broker -> equity None -> base_lot 0.10 full
    r = post(
        {
            "secret": "sek",
            "position_size": "10000000",
            "comment": "entry_long",
            "action": "buy",
            "entry": "1.08500",
            "stop": "1.08472",
        }
    )
    assert r.status_code == 200
    assert r.get_json()["target_lot"] == 0.10

    # tp1 -> half of base_lot
    r = post({"secret": "sek", "position_size": "5000000", "comment": "exit_long_tp1", "action": "sell"})
    assert r.get_json()["target_lot"] == 0.05

    # out-of-order: runner already flat, late tp1 cannot reopen
    post({"secret": "sek", "position_size": "0", "comment": "exit_long_runner", "action": "sell"})
    r = post({"secret": "sek", "position_size": "5000000", "comment": "exit_long_tp1", "action": "sell"})
    assert r.get_json()["target_lot"] == 0.0

    # bad secret rejected
    assert post({"secret": "nope", "position_size": "0", "comment": "x"}).status_code == 401
