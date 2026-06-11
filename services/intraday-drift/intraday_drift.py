"""Intraday EUR-weakness drift service — scheduled MetaApi demo executor.

Trades the validated 11:00-12:00 UTC EURUSD-weakness edge (option A): SHORT
EURUSD at 11:00 UTC with a broker-side protective stop sized to risk 1% of
equity, flat at 12:00 UTC. One trade per weekday. Independent of NovaTrade's
live engine and risk supervisor (a dedicated demo account, like the irb-bridge
and monthend-flow services).

Strategy logic lives in novatrade.strategies.intraday_drift (pure, tested). This
file is only the scheduler + broker glue.

SAFETY: defaults to IDF_DRY_RUN=true (paper) — it logs the exact order it would
place without touching MetaApi. Point IDF at a DEDICATED demo account and set
IDF_DRY_RUN=false to execute. The 1% protective stop is attached AT ENTRY as a
broker-side stop, so the risk cap holds even if this process is down at the
12:00 UTC time-exit. Closes only the position IT opened (tracked by id + a
magic-comment safety net).

Env:
  METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION (default london)
  IDF_DRY_RUN (default true), IDF_STOP_PIPS (default 20), IDF_RISK_FRAC (default 0.01),
  IDF_ENTRY_HOUR_UTC (default 11), IDF_EXIT_HOUR_UTC (default 12),
  IDF_POLL_SECONDS (default 60), IDF_PAPER_EQUITY (default 100000)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# make the repo importable when run from the service dir
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from novatrade.strategies.intraday_drift import (
    DriftConfig,
    build_order,
    is_entry_window,
    is_exit_time,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: S110 -- dotenv is optional; its absence is fine
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("intraday-drift")

UTC = timezone.utc
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "intraday_drift_state.json"
MAGIC = "IDF"

TOKEN = os.environ.get("METAAPI_TOKEN", "")
ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
REGION = os.environ.get("METAAPI_REGION", "london")
DRY_RUN = os.environ.get("IDF_DRY_RUN", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("IDF_POLL_SECONDS", "60"))
PAPER_EQUITY = float(os.environ.get("IDF_PAPER_EQUITY", "100000"))

CFG = DriftConfig(
    stop_pips=float(os.environ.get("IDF_STOP_PIPS", "20")),
    risk_frac=float(os.environ.get("IDF_RISK_FRAC", "0.01")),
    entry_hour_utc=int(os.environ.get("IDF_ENTRY_HOUR_UTC", "11")),
    exit_hour_utc=int(os.environ.get("IDF_EXIT_HOUR_UTC", "12")),
)


# ----------------------------- state -----------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state file unreadable; starting fresh")
    return {"date": None, "phase": "idle", "position_id": None}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, indent=2))


# ----------------------------- broker ----------------------------
class Broker:
    def __init__(self) -> None:
        self.conn = None

    async def connect(self) -> None:
        if DRY_RUN:
            log.info("IDF_DRY_RUN=true — MetaApi connection skipped (paper mode)")
            return
        from metaapi_cloud_sdk import MetaApi

        log.info("connecting to MetaApi account %s (region=%s)", ACCOUNT_ID, REGION)
        api = MetaApi(TOKEN)
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        await account.wait_connected()
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        self.conn = conn
        log.info("MetaApi connected and synchronized")

    async def equity(self) -> float:
        if DRY_RUN or self.conn is None:
            return PAPER_EQUITY
        info = await self.conn.get_account_information()
        return float(info["equity"])

    async def price(self) -> float:
        if DRY_RUN or self.conn is None:
            return 0.0
        p = await self.conn.get_symbol_price(CFG.symbol)
        # for a SELL we enter at bid; for BUY at ask
        return float(p["bid"]) if CFG.direction < 0 else float(p["ask"])

    async def open_trade(self, equity: float) -> tuple[str | None, dict]:
        """Open the single drift position with a broker-side protective stop.
        Returns (position_id, order_dict_for_logging)."""
        px = await self.price()
        order = build_order(equity, px if px else 1.0, CFG)
        info = {"side": order.side, "lot": order.lot, "stop": order.stop_price, "entry": px}
        if DRY_RUN or self.conn is None:
            log.info(
                "[PAPER] OPEN %s %s %.2f lots stop=%.5f (entry~%.5f)",
                order.side,
                order.symbol,
                order.lot,
                order.stop_price or 0.0,
                px,
            )
            return None, info
        pre = {p["id"] for p in await self.conn.get_positions() if p["symbol"] == CFG.symbol}
        opts = {"comment": MAGIC}
        try:
            if order.side == "BUY":
                await self.conn.create_market_buy_order(order.symbol, order.lot, order.stop_price, None, opts)
            else:
                await self.conn.create_market_sell_order(order.symbol, order.lot, order.stop_price, None, opts)
        except TypeError:  # SDK without options arg
            if order.side == "BUY":
                await self.conn.create_market_buy_order(order.symbol, order.lot, order.stop_price, None)
            else:
                await self.conn.create_market_sell_order(order.symbol, order.lot, order.stop_price, None)
        log.info("OPEN %s %s %.2f lots stop=%.5f", order.side, order.symbol, order.lot, order.stop_price or 0.0)
        await asyncio.sleep(2)
        post = await self.conn.get_positions()
        new = [p["id"] for p in post if p["symbol"] == CFG.symbol and p["id"] not in pre]
        return (new[0] if new else None), info

    async def position_open(self, pid: str | None) -> bool:
        if DRY_RUN or self.conn is None or pid is None:
            return pid is not None
        return any(p["id"] == pid for p in await self.conn.get_positions())

    async def close(self, pid: str | None) -> None:
        if DRY_RUN or self.conn is None:
            log.info("[PAPER] CLOSE position %s", pid)
            return
        live = {p["id"]: p for p in await self.conn.get_positions()}
        if pid in live:
            await self.conn.close_position(pid)
            log.info("CLOSE position %s (%s)", pid, live[pid]["symbol"])
        # safety net: close any EURUSD straggler tagged MAGIC
        for p in await self.conn.get_positions():
            if p["symbol"] == CFG.symbol and str(p.get("comment", "")).startswith(MAGIC):
                await self.conn.close_position(p["id"])
                log.info("CLOSE straggler %s (%s)", p["id"], p["symbol"])


# ----------------------------- loop ------------------------------
async def tick(broker: Broker) -> None:
    now = datetime.now(UTC)
    today_iso = now.date().isoformat()
    st = load_state()

    # OPEN phase: in the entry hour, on a weekday, not yet traded today
    if is_entry_window(now, CFG) and st.get("date") != today_iso:
        equity = await broker.equity()
        pid, info = await broker.open_trade(equity)
        log.info(
            "TRADE DAY %s: %s %.2f lots stop=%.5f equity=%.0f",
            today_iso,
            info["side"],
            info["lot"],
            info["stop"] or 0.0,
            equity,
        )
        save_state({"date": today_iso, "phase": "opened", "position_id": pid})
        return

    # CLOSE phase: at/after the exit hour, flatten the time-based position
    if st.get("date") == today_iso and st.get("phase") == "opened" and is_exit_time(now, CFG):
        pid = st.get("position_id")
        if await broker.position_open(pid) or DRY_RUN:
            log.info("EXIT %s — flattening drift position %s", now.strftime("%H:%M UTC"), pid)
            await broker.close(pid)
        else:
            log.info("position %s already closed (stop hit) — marking done", pid)
        st["phase"] = "closed"
        save_state(st)


async def main() -> None:
    log.info(
        "intraday-drift starting | DRY_RUN=%s | %s EURUSD %02d:00->%02d:00 UTC | stop=%.0fp risk=%.1f%% | poll=%ds",
        DRY_RUN,
        "SELL" if CFG.direction < 0 else "BUY",
        CFG.entry_hour_utc,
        CFG.exit_hour_utc,
        CFG.stop_pips,
        CFG.risk_frac * 100,
        POLL_SECONDS,
    )
    broker = Broker()
    await broker.connect()
    while True:
        try:
            await tick(broker)
        except Exception:
            log.exception("tick error")
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
