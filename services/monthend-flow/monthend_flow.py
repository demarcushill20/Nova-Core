"""Month-End Rebalancing-Flow service — scheduled MetaApi demo executor.

Trades the validated month-end flow edge (~4x/year): on the last business day of
each quarter it opens the non-JPY USD basket `hours_before_fix` hours before the
16:00 London WM/R fix, in the direction of the quarter-to-date S&P 500 return,
then flattens AT the fix. Independent of NovaTrade's live engine and risk
supervisor (a dedicated demo account, like services/irb-bridge).

Strategy logic lives in novatrade.strategies.month_end_flow (pure, tested). This
file is only the scheduler + broker glue.

SAFETY: defaults to MEF_DRY_RUN=true (paper) — it logs the exact orders it would
place without touching MetaApi. Point MEF at a DEDICATED demo account and set
MEF_DRY_RUN=false to execute. Closes only the positions IT opened (tracked by id
in the state file), so it is safe even on a shared account, though dedicated is
recommended.

Env:
  METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION (default london)
  MEF_DRY_RUN (default true), MEF_LEVERAGE (default 5.0),
  MEF_POLL_SECONDS (default 300), MEF_QUARTER_ENDS_ONLY (default true),
  MEF_PAPER_EQUITY (default 100000, equity used for sizing in dry-run)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# make the repo importable when run from the service dir
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from novatrade.strategies.month_end_flow import (
    MonthEndConfig,
    basket_orders,
    compute_signal,
    is_trade_day,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: S110 -- dotenv is optional; its absence is fine
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("monthend-flow")

LON = ZoneInfo("Europe/London")
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "monthend_state.json"
MAGIC = "MEF"

TOKEN = os.environ.get("METAAPI_TOKEN", "")
ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
REGION = os.environ.get("METAAPI_REGION", "london")
DRY_RUN = os.environ.get("MEF_DRY_RUN", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("MEF_POLL_SECONDS", "300"))
PAPER_EQUITY = float(os.environ.get("MEF_PAPER_EQUITY", "100000"))

CFG = MonthEndConfig(
    leverage=float(os.environ.get("MEF_LEVERAGE", "5.0")),
    quarter_ends_only=os.environ.get("MEF_QUARTER_ENDS_ONLY", "true").lower() == "true",
)
BASKET_SYMBOLS = {leg.symbol for leg in CFG.basket}


# ----------------------------- state -----------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state file unreadable; starting fresh")
    return {"date": None, "phase": "idle", "signal": None, "position_ids": []}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, indent=2))


# ----------------------------- signal ----------------------------
def fetch_signal(today) -> int | None:
    """Quarter-to-date S&P 500 sign with no look-ahead (yfinance ^GSPC daily)."""
    try:
        import yfinance as yf

        df = yf.download("^GSPC", period="120d", interval="1d", progress=False, auto_adjust=True)
        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)
        col = "Close" if "Close" in df.columns else "close"
        s = df[col].copy()
        s.index = [d.date() for d in s.index]
        return compute_signal(s, today)
    except Exception as e:  # pragma: no cover - network
        log.error("S&P signal fetch failed: %s", e)
        return None


# ----------------------------- broker ----------------------------
class Broker:
    def __init__(self) -> None:
        self.conn = None

    async def connect(self) -> None:
        if DRY_RUN:
            log.info("MEF_DRY_RUN=true — MetaApi connection skipped (paper mode)")
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

    async def open_basket(self, orders) -> list[str]:
        """Place all legs; return the ids of the positions we opened (diff of
        basket-symbol positions before/after, so we never touch others')."""
        if DRY_RUN or self.conn is None:
            for o in orders:
                log.info("[PAPER] OPEN %s %s %.2f lots", o.side, o.symbol, o.lot)
            return []
        pre = {p["id"] for p in await self.conn.get_positions() if p["symbol"] in BASKET_SYMBOLS}
        for o in orders:
            opts = {"comment": MAGIC}
            try:
                if o.side == "BUY":
                    await self.conn.create_market_buy_order(o.symbol, round(o.lot, 2), None, None, opts)
                else:
                    await self.conn.create_market_sell_order(o.symbol, round(o.lot, 2), None, None, opts)
            except TypeError:  # SDK without options arg
                if o.side == "BUY":
                    await self.conn.create_market_buy_order(o.symbol, round(o.lot, 2))
                else:
                    await self.conn.create_market_sell_order(o.symbol, round(o.lot, 2))
            log.info("OPEN %s %s %.2f lots", o.side, o.symbol, o.lot)
        await asyncio.sleep(2)
        post = await self.conn.get_positions()
        return [p["id"] for p in post if p["symbol"] in BASKET_SYMBOLS and p["id"] not in pre]

    async def close_ids(self, ids) -> None:
        if DRY_RUN or self.conn is None:
            log.info("[PAPER] CLOSE %d positions %s", len(ids), ids)
            return
        live = {p["id"]: p for p in await self.conn.get_positions()}
        for pid in ids:
            if pid in live:
                await self.conn.close_position(pid)
                log.info("CLOSE position %s (%s)", pid, live[pid]["symbol"])
        # safety net: also close any stragglers tagged MAGIC in basket symbols
        for p in await self.conn.get_positions():
            if p["symbol"] in BASKET_SYMBOLS and str(p.get("comment", "")).startswith(MAGIC):
                await self.conn.close_position(p["id"])
                log.info("CLOSE straggler %s (%s)", p["id"], p["symbol"])


# ----------------------------- loop ------------------------------
async def tick(broker: Broker) -> None:
    now = datetime.now(LON)
    today = now.date()
    st = load_state()
    today_iso = today.isoformat()

    if not is_trade_day(today, CFG):
        return
    open_hour = CFG.fix_hour_london - CFG.hours_before_fix
    after_open = now.hour >= open_hour
    after_fix = now.hour >= CFG.fix_hour_london

    # OPEN phase
    if after_open and not after_fix and st.get("date") != today_iso:
        sig = fetch_signal(today)
        if sig is None:
            log.error("no signal — skipping today's trade")
            save_state({"date": today_iso, "phase": "skipped", "signal": None, "position_ids": []})
            return
        equity = await broker.equity()
        orders = basket_orders(sig, equity, CFG)
        log.info(
            "TRADE DAY %s: signal=%+d (%s USD), equity=%.0f, %d legs",
            today_iso,
            sig,
            "SELL" if sig > 0 else "BUY",
            equity,
            len(orders),
        )
        ids = await broker.open_basket(orders)
        save_state({"date": today_iso, "phase": "opened", "signal": sig, "position_ids": ids})

    # CLOSE phase (at/after the fix)
    elif st.get("date") == today_iso and st.get("phase") == "opened" and after_fix:
        log.info("FIX reached — flattening basket (%d tracked positions)", len(st.get("position_ids", [])))
        await broker.close_ids(st.get("position_ids", []))
        st["phase"] = "closed"
        save_state(st)


async def main() -> None:
    log.info(
        "month-end-flow service starting | DRY_RUN=%s | leverage=%.1f | quarter_ends_only=%s | poll=%ds",
        DRY_RUN,
        CFG.leverage,
        CFG.quarter_ends_only,
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
