"""IRB baseline webhook bridge — TradingView strategy alerts -> MetaApi MT5 demo.

Receives a JSON webhook on each TradingView order fill, validates a shared
secret, maps the strategy's net position to a lot size, and reconciles the
MT5 demo account to match.

Runs the Rob Hoffman IRB v5 Relaxed Reliable Build (5min Champion) baseline
on EURUSD against a dedicated demo account. Independent of NovaTrade's live
runtime — no HardRiskSupervisor, no shared state.

Run:  python3 irb_bridge.py
Config is read from .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE / "irb_bridge.log"),
    ],
)
log = logging.getLogger("irb")

for noisy in ("socketio", "engineio", "metaapi", "websockets", "werkzeug"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Pure sizing helpers (no MetaApi / env dependency — importable for tests).
# ---------------------------------------------------------------------------
@dataclass
class SizingCfg:
    risk_pct: float
    contract_size: float
    min_lot: float
    max_lot: float
    lot_step: float
    base_lot: float


def _round_step(x: float, step: float) -> float:
    """Round x to the nearest broker lot step."""
    return round(round(x / step) * step, 8)


def risk_based_lot(entry, stop, equity, cfg: SizingCfg) -> float:
    """Lot that risks cfg.risk_pct of `equity` if price travels entry->stop.

    Falls back to cfg.base_lot when entry/stop/equity are missing or invalid,
    so a bad alert or a failed equity read never blocks a trade — it just
    sizes conservatively at the fixed base lot."""
    try:
        entry = float(entry)
        stop = float(stop)
        equity = float(equity)
    except (TypeError, ValueError):
        return cfg.base_lot
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or equity <= 0:
        return cfg.base_lot
    risk_cash = equity * (cfg.risk_pct / 100.0)
    raw = risk_cash / (cfg.contract_size * stop_dist)
    lot = _round_step(raw, cfg.lot_step)
    return round(max(cfg.min_lot, min(lot, cfg.max_lot)), 2)


def compute_desired_lot(state: dict, position_size: float, comment: str, entry, stop, equity, cfg: SizingCfg):
    """Return (signed_desired_lot, new_state).

    Entry alerts (comment startswith 'entry') are the ONLY events that set the
    reference position and the risk-based full lot. Exit alerts merely scale the
    stored full lot by the surviving fraction, and that fraction is monotonically
    non-increasing within a trade — so an out-of-order or duplicate exit alert can
    never re-open or grow a position."""
    comment = comment or ""
    cur_sign = 1 if position_size > 0 else -1 if position_size < 0 else 0

    if comment.startswith("entry") and cur_sign != 0:
        full_lot = risk_based_lot(entry, stop, equity, cfg)
        new_state = {
            "side": cur_sign,
            "entry_units": abs(position_size),
            "full_lot": full_lot,
            "last_fraction": 1.0,
        }
        return round(cur_sign * full_lot, 2), new_state

    # Exit / partial / time-stop alert.
    side = state.get("side", 0)
    entry_units = state.get("entry_units", 0.0) or 0.0
    full_lot = state.get("full_lot", 0.0) or 0.0
    if side == 0 or entry_units <= 0 or full_lot <= 0:
        return 0.0, {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}

    frac = 0.0 if position_size == 0 else abs(position_size) / entry_units
    frac = min(frac, state.get("last_fraction", 1.0))  # monotonic non-increasing
    frac = max(0.0, min(frac, 1.0))

    new_state = dict(state)
    new_state["last_fraction"] = frac
    if frac <= 0.0:
        new_state["side"] = 0
    desired = round(side * _round_step(full_lot * frac, cfg.lot_step), 2)
    return desired, new_state


# Env reads use .get() so the module imports cleanly under pytest (no secrets
# required); live runs supply real values via .env / systemd.
SECRET = os.environ.get("IRB_WEBHOOK_SECRET", "")
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
METAAPI_REGION = os.environ.get("METAAPI_REGION", "london")
SYMBOL = os.environ.get("IRB_SYMBOL", "EURUSD")
BASE_LOT = float(os.environ.get("IRB_BASE_LOT", "0.10"))
BIND_HOST = os.environ.get("IRB_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("IRB_BIND_PORT", "8081"))
DRY_RUN = os.environ.get("IRB_DRY_RUN", "false").lower() == "true"

RISK_PCT = float(os.environ.get("IRB_RISK_PCT", "1.0"))
CONTRACT_SIZE = float(os.environ.get("IRB_CONTRACT_SIZE", "100000"))
MIN_LOT = float(os.environ.get("IRB_MIN_LOT", "0.01"))
MAX_LOT = float(os.environ.get("IRB_MAX_LOT", "50.0"))
LOT_STEP = float(os.environ.get("IRB_LOT_STEP", "0.01"))

SIZING = SizingCfg(
    risk_pct=RISK_PCT,
    contract_size=CONTRACT_SIZE,
    min_lot=MIN_LOT,
    max_lot=MAX_LOT,
    lot_step=LOT_STEP,
    base_lot=BASE_LOT,
)

STATE_FILE = BASE / "bridge_state.json"
LOT_EPSILON = 0.005


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("bridge_state.json corrupt — resetting")
    return {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def reconcile(conn, desired: float, symbol: str, epsilon: float, lock):
    """Drive the broker net position for `symbol` to `desired` lots.

    Serialized by `lock` so concurrent alerts never act on stale state: each
    invocation re-reads positions only after the previous one has fully
    completed (orders acked)."""
    async with lock:
        positions = await conn.get_positions()
        sym = [p for p in positions if p["symbol"] == symbol]
        current = sum((p["volume"] if p["type"] == "POSITION_TYPE_BUY" else -p["volume"]) for p in sym)
        log.info("reconcile %s: current %+.2f -> desired %+.2f", symbol, current, desired)

        same_sign = (current >= 0) == (desired >= 0)
        if not same_sign or abs(desired) < epsilon:
            for p in sym:
                await conn.close_position(p["id"])
            if abs(desired) < epsilon:
                return
            current = 0.0
            sym = []

        delta = desired - current
        if abs(delta) < epsilon:
            log.info("already in line — no order")
            return
        if delta > 0 and desired > 0:
            await conn.create_market_buy_order(symbol, round(delta, 2))
        elif delta < 0 and desired < 0:
            await conn.create_market_sell_order(symbol, round(-delta, 2))
        else:
            await _reduce(conn, sym, abs(delta), epsilon)


async def _reduce(conn, sym_positions, volume_to_close: float, epsilon: float) -> None:
    remaining = volume_to_close
    for p in sym_positions:
        if remaining < epsilon:
            break
        chunk = min(p["volume"], remaining)
        if chunk >= p["volume"] - epsilon:
            await conn.close_position(p["id"])
        else:
            await conn.close_position_partially(p["id"], round(chunk, 2))
        remaining -= chunk


class Broker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._connection = None
        self._lock: asyncio.Lock | None = None  # created on the broker's own loop
        self._ready = threading.Event()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._lock = asyncio.Lock()
        if not DRY_RUN:
            self.loop.run_until_complete(self._connect())
        else:
            log.info("DRY_RUN=true — MetaApi connection skipped")
            self._ready.set()
        self.loop.run_forever()

    async def _connect(self) -> None:
        from metaapi_cloud_sdk import MetaApi

        log.info("connecting to MetaApi account %s (region=%s) ...", METAAPI_ACCOUNT_ID, METAAPI_REGION)
        api = MetaApi(METAAPI_TOKEN)
        account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        await account.wait_connected()
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        self._connection = conn
        self._ready.set()
        log.info("MetaApi connected and synchronized")

    def submit_target(self, desired_lot: float) -> None:
        asyncio.run_coroutine_threadsafe(self._reconcile(desired_lot), self.loop)

    def get_equity(self):
        """Live account equity in USD, or None if unavailable (caller falls
        back to base lot). In DRY_RUN, returns IRB_DRY_EQUITY (default 100k)."""
        if DRY_RUN:
            return float(os.environ.get("IRB_DRY_EQUITY", "100000"))
        if self._connection is None:
            log.error("get_equity: MetaApi not connected")
            return None
        fut = asyncio.run_coroutine_threadsafe(self._get_equity(), self.loop)
        try:
            return fut.result(timeout=10)
        except Exception:
            log.exception("get_equity failed")
            return None

    async def _get_equity(self):
        info = await self._connection.get_account_information()
        return float(info["equity"])

    async def _reconcile(self, desired: float) -> None:
        try:
            if DRY_RUN:
                log.info("[DRY_RUN] would reconcile %s to %+.2f lots", SYMBOL, desired)
                return
            if self._connection is None:
                log.error("MetaApi not connected — dropping reconcile %s", desired)
                return
            await reconcile(self._connection, desired, SYMBOL, LOT_EPSILON, self._lock)
        except Exception:
            log.exception("reconcile failed for desired=%s", desired)


# Created in __main__ only — instantiating Broker() spawns a thread (and a live
# MetaApi connection), which must not happen at import time under pytest.
broker: Broker | None = None


app = Flask(__name__)


@app.get("/irb/health")
def health():
    return jsonify(
        status="ok",
        dry_run=DRY_RUN,
        symbol=SYMBOL,
        risk_pct=RISK_PCT,
        base_lot=BASE_LOT,
        max_lot=MAX_LOT,
        account=METAAPI_ACCOUNT_ID,
        region=METAAPI_REGION,
    )


@app.post("/irb/webhook")
def webhook():
    raw = request.get_data(as_text=True)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("rejected: body is not JSON: %s", raw[:200])
        return jsonify(error="invalid json"), 400

    if payload.get("secret") != SECRET:
        log.warning("rejected: bad secret from %s", request.remote_addr)
        return jsonify(error="unauthorized"), 401

    try:
        position_size = float(payload.get("position_size", "nan"))
    except ValueError:
        log.warning("rejected: bad position_size %r", payload.get("position_size"))
        return jsonify(error="bad position_size"), 400
    if math.isnan(position_size):
        return jsonify(error="missing position_size"), 400

    comment = str(payload.get("comment", ""))

    def _opt_float(key):
        try:
            return float(payload[key])
        except (KeyError, TypeError, ValueError):
            return None

    entry = _opt_float("entry")
    stop = _opt_float("stop")

    equity = broker.get_equity() if broker is not None else None
    state = load_state()
    desired, new_state = compute_desired_lot(state, position_size, comment, entry, stop, equity, SIZING)
    save_state(new_state)

    log.info(
        "webhook ok: pos_size=%s comment=%r action=%r entry=%s stop=%s equity=%s -> target %+.2f lots",
        position_size,
        comment,
        payload.get("action"),
        entry,
        stop,
        equity,
        desired,
    )

    if broker is not None:
        broker.submit_target(desired)
    return jsonify(status="accepted", target_lot=round(desired, 2)), 200


if __name__ == "__main__":
    broker = Broker()
    log.info("IRB bridge starting on %s:%s (dry_run=%s, symbol=%s)", BIND_HOST, BIND_PORT, DRY_RUN, SYMBOL)
    app.run(host=BIND_HOST, port=BIND_PORT)
