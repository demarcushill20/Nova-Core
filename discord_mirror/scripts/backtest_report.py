"""Backtest report on the signal provider's claimed accuracy.

Reads signals + status_events from SQLite (populated by backfill_history.py)
and reconstructs per-signal outcomes based on what the provider himself posted
afterwards ("TP1 SMACKED", "ALL TPs SMASHED", "stopped out").

Outcome classification per signal:
- ALL_TPS  : the message "ALL TPs SMASHED" was posted, OR every individual TP fired
- PARTIAL  : at least TP1 hit, but not all TPs (some closed via SL→BE or no further reports)
- SL       : stopped out before any TP hit
- UNRESOLVED: signal is recent enough that no terminal status was posted yet

Note: this is "if the provider's reports are accurate, this is the win rate."
It does NOT independently verify against actual broker prices — for that, we'd
need to pull historical XAUUSD ticks and check whether price genuinely traded
through SL or each TP before the provider posted his status.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from discord_mirror.config import load_config
from discord_mirror.storage import Storage


async def main(top_n_unresolved: int = 5) -> None:
    cfg = load_config(Path("config.yaml"))
    s = Storage(cfg.storage.db_path)
    await s.init()
    db = s._conn

    # All OPEN signals in chronological order
    cur = await db.execute(
        "SELECT s.id, s.symbol, s.direction, s.sl, s.tps_json, r.ts "
        "FROM signals s JOIN raw_messages r ON r.id = s.raw_message_id "
        "WHERE s.action = 'OPEN' ORDER BY r.ts ASC"
    )
    signals = [dict(row) for row in await cur.fetchall()]

    # All status events
    cur = await db.execute("SELECT kind, tp_index, ts FROM status_events ORDER BY ts ASC")
    events = [dict(row) for row in await cur.fetchall()]

    if not signals:
        print("No OPEN signals in database. Run backfill_history.py first.")
        await s.close()
        return

    outcomes: list[dict] = []
    for i, sig in enumerate(signals):
        sig_ts = sig["ts"]
        next_sig_ts = signals[i + 1]["ts"] if i + 1 < len(signals) else None
        between = [e for e in events if e["ts"] > sig_ts and (next_sig_ts is None or e["ts"] < next_sig_ts)]
        tps_in_signal = len(json.loads(sig["tps_json"]))
        tp_hits = {e["tp_index"] for e in between if e["kind"] == "TP_HIT" and e["tp_index"]}
        sl_hits = [e for e in between if e["kind"] == "SL_HIT"]
        all_tps = [e for e in between if e["kind"] == "ALL_TPS_HIT"]

        if all_tps or (tps_in_signal > 0 and len(tp_hits) >= tps_in_signal):
            outcome = "ALL_TPS"
        elif sl_hits and not tp_hits:
            outcome = "SL"
        elif tp_hits:
            outcome = "PARTIAL"
        else:
            outcome = "UNRESOLVED"

        outcomes.append(
            {
                "id": sig["id"],
                "ts": sig_ts,
                "symbol": sig["symbol"],
                "direction": sig["direction"],
                "tps_in_signal": tps_in_signal,
                "tps_hit": sorted(tp_hits),
                "outcome": outcome,
            }
        )

    # Summary
    bucket = Counter(o["outcome"] for o in outcomes)
    resolved = sum(bucket[k] for k in ("ALL_TPS", "PARTIAL", "SL"))
    total = len(outcomes)
    win_strict = bucket["ALL_TPS"]  # full win
    win_loose = bucket["ALL_TPS"] + bucket["PARTIAL"]
    losses = bucket["SL"]

    by_symbol = Counter(o["symbol"] for o in outcomes if o["symbol"])
    tp_hit_dist = Counter(len(o["tps_hit"]) for o in outcomes)

    print("=" * 60)
    print("BACKTEST REPORT — provider's claimed performance")
    print("=" * 60)
    print(f"signals: {total}  (resolved: {resolved}, unresolved: {bucket['UNRESOLVED']})")
    print(f"  ALL_TPS hit:  {bucket['ALL_TPS']:>4}  ({pct(bucket['ALL_TPS'], resolved)})")
    print(f"  PARTIAL:      {bucket['PARTIAL']:>4}  ({pct(bucket['PARTIAL'], resolved)})")
    print(f"  SL hit:       {bucket['SL']:>4}  ({pct(bucket['SL'], resolved)})")
    print(f"  UNRESOLVED:   {bucket['UNRESOLVED']:>4}")
    print()
    print(f"win rate (strict / full TP run):  {pct(win_strict, resolved)}")
    print(f"win rate (loose / TP1 or better): {pct(win_loose, resolved)}")
    print(f"loss rate (SL before any TP):     {pct(losses, resolved)}")
    print()
    if by_symbol:
        print("by symbol:")
        for sym, n in by_symbol.most_common():
            print(f"  {sym:<8} {n}")
        print()
    print("TPs hit per signal (distribution):")
    for k in sorted(tp_hit_dist):
        print(f"  hit {k} TPs: {tp_hit_dist[k]} signals")
    print()
    if bucket["UNRESOLVED"] and top_n_unresolved:
        print(f"most recent unresolved signals (last {top_n_unresolved}):")
        for o in [x for x in outcomes if x["outcome"] == "UNRESOLVED"][-top_n_unresolved:]:
            print(f"  {o['ts']}  {o['symbol']} {o['direction']}  tps_hit={o['tps_hit']}")

    await s.close()


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    asyncio.run(main(n))
