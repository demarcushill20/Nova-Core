#!/usr/bin/env python3
"""Fetch FTMO demo-challenge deal history from MetaApi for trade-parity audit.

Pulls deals + history orders over the last N days and writes two CSVs:
  OUTPUT/parity/live_deals_<stamp>.csv      — every deal (entry + exit legs)
  OUTPUT/parity/live_trades_<stamp>.csv     — aggregated per position (one row per completed trade)

Usage:
    python3 scripts/fetch_ftmo_deal_history.py --days 3 --env-file OUTPUT/novatrade.env.updated
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.adapter.metaapi_sdk_singleton import get_metaapi_sdk
from novatrade.config import NovaTradeCfg


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def _fetch(cfg, days: int, out_dir: Path) -> dict:
    api = get_metaapi_sdk(
        token=cfg.metaapi.token,
        domain=cfg.metaapi.domain,
        region=cfg.metaapi.region,
        application=cfg.metaapi.application,
    )
    print(f"[fetch] account_id={cfg.metaapi.account_id} region={cfg.metaapi.region}")
    account = await api.metatrader_account_api.get_account(cfg.metaapi.account_id)
    if account.state != "DEPLOYED":
        print(f"[fetch] deploying account (state={account.state})")
        await account.deploy()
        await account.wait_deployed()
    await account.wait_connected()
    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    print(f"[fetch] window {start.isoformat()} → {end.isoformat()}")

    deals_resp = await conn.get_deals_by_time_range(start, end, 0, 1000)
    orders_resp = await conn.get_history_orders_by_time_range(start, end, 0, 1000)

    deals = deals_resp.get("deals", []) if isinstance(deals_resp, dict) else getattr(deals_resp, "deals", [])
    history_orders = (
        orders_resp.get("historyOrders", [])
        if isinstance(orders_resp, dict)
        else getattr(orders_resp, "history_orders", [])
    )

    print(f"[fetch] deals={len(deals)} history_orders={len(history_orders)}")

    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    stamp = end.strftime("%Y%m%d_%H%M%S")

    # Raw deals
    deals_csv = out_dir / f"live_deals_{stamp}.csv"
    if deals:
        keys = sorted({k for d in deals for k in d})
        with deals_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for d in deals:
                w.writerow({k: _stringify(d.get(k)) for k in keys})
    else:
        deals_csv.write_text("")
    print(f"[fetch] wrote {deals_csv}")

    # Raw history orders (contain SL/TP and original placement intent)
    orders_csv = out_dir / f"live_orders_{stamp}.csv"
    if history_orders:
        keys = sorted({k for o in history_orders for k in o})
        with orders_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for o in history_orders:
                w.writerow({k: _stringify(o.get(k)) for k in keys})
    else:
        orders_csv.write_text("")
    print(f"[fetch] wrote {orders_csv}")

    # Aggregate: one trade per positionId
    trades = _aggregate_trades(deals, history_orders)
    trades_csv = out_dir / f"live_trades_{stamp}.csv"
    trade_fields = [
        "position_id",
        "symbol",
        "side",
        "volume",
        "entry_time",
        "entry_price",
        "sl",
        "tp",
        "exit_time",
        "exit_price",
        "exit_reason",
        "profit",
        "commission",
        "swap",
        "net_pnl",
        "outcome",
    ]
    with trades_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=trade_fields)
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"[fetch] wrote {trades_csv} ({len(trades)} trades)")

    # Also dump raw JSON for debugging / audit
    raw_json = out_dir / f"live_raw_{stamp}.json"
    raw_json.write_text(
        json.dumps(
            {"deals": deals, "history_orders": history_orders},
            default=str,
            indent=2,
        )
    )
    print(f"[fetch] wrote {raw_json}")

    await conn.close()
    return {"deals": len(deals), "orders": len(history_orders), "trades": len(trades), "out_dir": str(out_dir)}


def _stringify(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _aggregate_trades(deals: list[dict], orders: list[dict]) -> list[dict]:
    """Fold buy+sell deal pairs on the same positionId into one trade row."""
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        pid = d.get("positionId") or d.get("position_id")
        if pid:
            by_pos[pid].append(d)

    # Index orders by positionId for SL/TP lookup
    orders_by_pos: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        pid = o.get("positionId") or o.get("position_id")
        if pid:
            orders_by_pos[pid].append(o)

    trades = []
    for pid, legs in by_pos.items():
        legs_sorted = sorted(legs, key=lambda d: d.get("time") or "")
        entry = next(
            (leg for leg in legs_sorted if (leg.get("entryType") or leg.get("entry_type")) == "DEAL_ENTRY_IN"),
            legs_sorted[0],
        )
        exits = [leg for leg in legs_sorted if (leg.get("entryType") or leg.get("entry_type")) == "DEAL_ENTRY_OUT"]
        last_exit = exits[-1] if exits else None

        pos_orders = orders_by_pos.get(pid, [])
        sl = tp = None
        for o in pos_orders:
            sl = o.get("stopLoss") or sl
            tp = o.get("takeProfit") or tp

        profit = sum(float(leg.get("profit") or 0) for leg in legs_sorted)
        commission = sum(float(leg.get("commission") or 0) for leg in legs_sorted)
        swap = sum(float(leg.get("swap") or 0) for leg in legs_sorted)
        net = profit + commission + swap

        trades.append(
            {
                "position_id": pid,
                "symbol": entry.get("symbol"),
                "side": _side_from_type(entry.get("type")),
                "volume": entry.get("volume"),
                "entry_time": entry.get("time"),
                "entry_price": entry.get("price"),
                "sl": sl,
                "tp": tp,
                "exit_time": last_exit.get("time") if last_exit else None,
                "exit_price": last_exit.get("price") if last_exit else None,
                "exit_reason": last_exit.get("reason") if last_exit else None,
                "profit": profit,
                "commission": commission,
                "swap": swap,
                "net_pnl": net,
                "outcome": "WIN" if net > 0 else ("LOSS" if net < 0 else "FLAT") if last_exit else "OPEN",
            }
        )
    return sorted(trades, key=lambda t: t.get("entry_time") or "")


def _side_from_type(t) -> str:
    s = str(t or "").upper()
    if "BUY" in s:
        return "BUY"
    if "SELL" in s:
        return "SELL"
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--env-file", default="OUTPUT/novatrade.env.updated")
    ap.add_argument("--out-dir", default="OUTPUT/parity")
    args = ap.parse_args()

    _load_env_file(Path(args.env_file))
    cfg = NovaTradeCfg.load(env_file=Path(args.env_file))
    errs = cfg.validate()
    if errs:
        for e in errs:
            print(f"CONFIG ERROR: {e}")
        return 1

    summary = asyncio.run(_fetch(cfg, args.days, Path(args.out_dir)))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
