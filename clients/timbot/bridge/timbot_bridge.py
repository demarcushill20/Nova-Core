"""TIMBOT webhook bridge — TradingView strategy alerts -> MetaApi MT5 demo.

Receives a JSON webhook on each TradingView order fill, validates a shared
secret, maps the strategy's net position to a lot size, and reconciles the
MT5 demo account to match.

Isolated client infrastructure: no NovaTrade code, dedicated demo account.

Run:  python3 timbot_bridge.py
Config is read from .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
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
        logging.FileHandler(BASE / "timbot_bridge.log"),
    ],
)
log = logging.getLogger("timbot")

# Quiet the very chatty third-party loggers (MetaApi SDK / socket.io packet
# spam) so the service log stays readable over a multi-week unattended run.
for noisy in ("socketio", "engineio", "metaapi", "websockets", "werkzeug"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SECRET = os.environ["TIMBOT_WEBHOOK_SECRET"]
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
METAAPI_REGION = os.environ.get("METAAPI_REGION", "new-york")
SYMBOL = os.environ.get("TIMBOT_SYMBOL", "XAUUSD")
BASE_LOT = float(os.environ.get("TIMBOT_BASE_LOT", "0.10"))
BIND_HOST = os.environ.get("TIMBOT_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("TIMBOT_BIND_PORT", "8080"))
DRY_RUN = os.environ.get("TIMBOT_DRY_RUN", "true").lower() == "true"

STATE_FILE = BASE / "bridge_state.json"
LOT_EPSILON = 0.005  # ignore reconciliation deltas below half a micro-lot


# ──────────────────────────────────────────────────────────────────────────
#  Persistent state — the strategy's "full position" reference per entry
# ──────────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("bridge_state.json corrupt — resetting")
    return {"reference_size": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────────────────────────────────
#  Broker — owns an asyncio loop in a background thread, keeps the MetaApi
#  RPC connection warm, and reconciles the demo account to a target lot.
# ──────────────────────────────────────────────────────────────────────────
class Broker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._connection = None
        self._ready = threading.Event()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        if not DRY_RUN:
            self.loop.run_until_complete(self._connect())
        else:
            log.info("DRY_RUN=true — MetaApi connection skipped")
            self._ready.set()
        self.loop.run_forever()

    async def _connect(self) -> None:
        # Imported lazily so DRY_RUN works without the SDK installed.
        from metaapi_cloud_sdk import MetaApi

        log.info("connecting to MetaApi account %s ...", METAAPI_ACCOUNT_ID)
        api = MetaApi(METAAPI_TOKEN)
        account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        await account.wait_connected()
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        self._connection = conn
        self._ready.set()
        log.info("MetaApi connected and synchronized")

    # Called from the Flask thread — schedules a reconcile on the loop.
    def submit_target(self, desired_lot: float) -> None:
        asyncio.run_coroutine_threadsafe(self._reconcile(desired_lot), self.loop)

    async def _reconcile(self, desired: float) -> None:
        """Drive the demo account's net SYMBOL position to `desired` lots
        (signed: positive long, negative short)."""
        try:
            if DRY_RUN:
                log.info("[DRY_RUN] would reconcile %s to %+.2f lots", SYMBOL, desired)
                return

            conn = self._connection
            if conn is None:
                log.error("MetaApi not connected — dropping reconcile %s", desired)
                return
            positions = await conn.get_positions()
            sym = [p for p in positions if p["symbol"] == SYMBOL]
            current = sum((p["volume"] if p["type"] == "POSITION_TYPE_BUY" else -p["volume"]) for p in sym)
            log.info("reconcile %s: current %+.2f -> desired %+.2f", SYMBOL, current, desired)

            same_sign = (current >= 0) == (desired >= 0)

            # Reversal or close: flatten everything first.
            if not same_sign or abs(desired) < LOT_EPSILON:
                for p in sym:
                    await conn.close_position(p["id"])
                if abs(desired) < LOT_EPSILON:
                    return
                current = 0.0

            delta = desired - current
            if abs(delta) < LOT_EPSILON:
                log.info("already in line — no order")
                return

            if delta > 0 and desired > 0:  # grow long
                await conn.create_market_buy_order(SYMBOL, round(delta, 2))
            elif delta < 0 and desired < 0:  # grow short
                await conn.create_market_sell_order(SYMBOL, round(-delta, 2))
            else:  # shrink toward target
                await self._reduce(conn, sym, abs(delta))
        except Exception:
            log.exception("reconcile failed for desired=%s", desired)

    async def _reduce(self, conn, sym_positions, volume_to_close: float) -> None:
        remaining = volume_to_close
        for p in sym_positions:
            if remaining < LOT_EPSILON:
                break
            chunk = min(p["volume"], remaining)
            if chunk >= p["volume"] - LOT_EPSILON:
                await conn.close_position(p["id"])
            else:
                await conn.close_position_partially(p["id"], round(chunk, 2))
            remaining -= chunk


broker = Broker()


# ──────────────────────────────────────────────────────────────────────────
#  Position mapping — TradingView strategy.position_size (oz) -> lot
# ──────────────────────────────────────────────────────────────────────────
def map_to_lot(position_size: float) -> float:
    """Map the strategy's net position to a demo lot.

    A fresh entry — position going from flat, or flipping sign — sets the
    reference 'full position'. Partial exits scale BASE_LOT by the surviving
    fraction. Derived purely from position_size, so the TradingView alert
    only needs that one placeholder."""
    state = load_state()
    ref = state.get("reference_size")
    last = float(state.get("last_size", 0.0) or 0.0)

    cur_sign = 1 if position_size > 0 else -1 if position_size < 0 else 0
    last_sign = 1 if last > 0 else -1 if last < 0 else 0

    new_entry = cur_sign != 0 and (last_sign == 0 or cur_sign != last_sign)
    if new_entry or ref is None:
        ref = abs(position_size) or None
    elif cur_sign != 0 and ref and abs(position_size) > ref:
        ref = abs(position_size)  # position grew — re-baseline

    state["reference_size"] = ref
    state["last_size"] = position_size
    save_state(state)

    if not ref or cur_sign == 0:
        return 0.0
    return cur_sign * BASE_LOT * (abs(position_size) / ref)


# ──────────────────────────────────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/timbot/health")
def health():
    return jsonify(status="ok", dry_run=DRY_RUN, symbol=SYMBOL, base_lot=BASE_LOT)


@app.post("/timbot/webhook")
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

    desired = map_to_lot(position_size)
    log.info(
        "webhook ok: pos_size=%s comment=%r action=%r -> target %+.2f lots",
        position_size,
        payload.get("comment"),
        payload.get("action"),
        desired,
    )

    broker.submit_target(desired)
    return jsonify(status="accepted", target_lot=round(desired, 2)), 200


if __name__ == "__main__":
    log.info("TIMBOT bridge starting on %s:%s (dry_run=%s, symbol=%s)", BIND_HOST, BIND_PORT, DRY_RUN, SYMBOL)
    app.run(host=BIND_HOST, port=BIND_PORT)
