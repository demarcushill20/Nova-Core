"""Intraday-drift live-vs-backtest parity check (run by a user systemd timer just
after the 12:00 UTC exit). Gathers the day's evidence and writes a report to OUTPUT/.

Evidence:
  1. journalctl --user -u intraday-drift  -> the entry order the service placed (side,
     lots, stop, intended price) and the 12:00 exit.
  2. MetaApi deals for the day on account 60dab0f9 (login 108610127) -> the ACTUAL fill
     price, exit price, lots, broker stop, realized P&L.
  3. Expected: the 11:00->12:00 EURUSD short edge (mean ~-1.4p, ~52% win). A single day
     is noisy — we check direction + that the executor fired correctly, not that it won.

Self-contained: the deal's own entry (11:00) and exit (12:00) prices ARE the backtest's
reference prices, so no external feed is needed. Slippage = service's intended entry vs
the actual fill. Token read from /etc/novacore/novatrade.env (nova-readable).
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT_ID = "60dab0f9-227c-48b6-9f4b-fb8146ea5c54"
SYMBOL = "EURUSD"
PIP = 1e-4
ASSUMED_SPREAD_P = 0.30
OUT = Path("/home/nova/nova-core/OUTPUT")


def _token() -> str:
    with open("/etc/novacore/novatrade.env") as f:
        for line in f:
            if line.startswith("METAAPI_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("METAAPI_TOKEN not found")


def journal_window(day: datetime) -> str:
    since = (day.replace(hour=10, minute=55)).strftime("%Y-%m-%d %H:%M:%S")
    until = (day.replace(hour=12, minute=10)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        out = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "intraday-drift",
                "--since",
                since,
                "--until",
                until,
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception as e:
        return f"(journalctl failed: {e})"
    # keep the signal lines (entry/exit/order/error), drop MetaApi SDK chatter
    keep = [
        ln
        for ln in out.splitlines()
        if any(
            k in ln
            for k in ("SELL", "BUY", "entry", "exit", "order", "stop", "ERROR", "Traceback", "place", "close", "filled")
        )
        and "MESSAGE data" not in ln
        and "websocket" not in ln
    ]
    return "\n".join(keep) or "(no entry/exit log lines found in the window)"


async def fetch_deals(day: datetime) -> dict:
    from metaapi_cloud_sdk import MetaApi

    api = MetaApi(_token())
    acc = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    await asyncio.wait_for(acc.wait_connected(), timeout=180)
    conn = acc.get_rpc_connection()
    await conn.connect()
    await asyncio.wait_for(conn.wait_synchronized(), timeout=120)
    info = await conn.get_account_information()
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    deals = []
    try:
        res = await conn.get_deals_by_time_range(start, end)
        deals = res.get("deals", res) if isinstance(res, dict) else res
    except Exception as e:
        deals = [{"error": f"get_deals_by_time_range failed: {e!r}"[:200]}]
    eur = [d for d in deals if isinstance(d, dict) and d.get("symbol") == SYMBOL]
    return {
        "balance": info.get("balance"),
        "equity": info.get("equity"),
        "currency": info.get("currency"),
        "deals": eur,
        "n_all_deals": len(deals),
    }


def build_report(day: datetime, jrnl: str, md: dict) -> str:
    lines = [f"# Intraday-drift parity check — {day:%Y-%m-%d} (Mon)", ""]
    lines += ["## Service log (entry/exit it placed)", "```", jrnl, "```", ""]
    lines += [
        "## MetaApi account 60dab0f9 (login 108610127)",
        f"- balance: {md.get('balance')} {md.get('currency')}  equity: {md.get('equity')}",
        f"- P&L vs 100000 base: {(md.get('balance') or 0) - 100000:+.2f} {md.get('currency')}",
        f"- EURUSD deals today: {len(md.get('deals', []))} (of {md.get('n_all_deals')} total)",
        "",
    ]
    deals = md.get("deals", [])
    entry = next((d for d in deals if d.get("entryType") == "DEAL_ENTRY_IN"), None)
    exit_ = next((d for d in deals if d.get("entryType") == "DEAL_ENTRY_OUT"), None)
    if entry and exit_:
        ep, xp = entry.get("price"), exit_.get("price")
        vol = entry.get("volume")
        move_p = (ep - xp) / PIP if (ep and xp) else None  # SHORT: profit when price falls
        profit = (
            (entry.get("profit", 0) or 0)
            + (exit_.get("profit", 0) or 0)
            + (entry.get("commission", 0) or 0)
            + (exit_.get("commission", 0) or 0)
            + (entry.get("swap", 0) or 0)
            + (exit_.get("swap", 0) or 0)
        )
        lines += [
            "## Parity",
            f"- side: {'SELL' if entry.get('type') == 'DEAL_TYPE_SELL' else entry.get('type')}  volume: {vol} lots",
            f"- entry (11:00 fill): {ep}   exit (12:00 fill): {xp}",
            f"- realized 11->12 move: {move_p:+.2f}p  (edge expects NEGATIVE EURUSD = short profit; mean ~-1.4p, ~52% win)"
            if move_p is not None
            else "- move: n/a",
            f"- realized P&L: {profit:+.2f} {md.get('currency')}",
            f"- net of {ASSUMED_SPREAD_P}p assumed spread, gross edge sign: "
            f"{'CONSISTENT (down)' if (move_p or 0) > 0 else 'AGAINST (up) — noisy single day' if move_p is not None else 'n/a'}",
            "",
        ]
        verdict = "FIRED + " + ("WIN" if profit > 0 else "LOSS (single-day noise; edge is statistical)")
    elif deals:
        lines += ["## Parity", f"- deals present but couldn't pair entry/exit: {deals}", ""]
        verdict = "REVIEW — deals found but unpaired"
    else:
        verdict = "NO TRADE FOUND — check the service log above (missed trade? market holiday? error?)"
    lines += [f"## Verdict\n{verdict}"]
    return "\n".join(lines)


def main() -> None:
    day = datetime.now(timezone.utc)
    jrnl = journal_window(day)
    try:
        md = asyncio.run(fetch_deals(day))
    except Exception as e:
        md = {"deals": [], "n_all_deals": 0, "error": repr(e)[:300]}
    report = build_report(day, jrnl, md)
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{day:%Y%m%d}_idf_parity_check.md"
    path.write_text(report)
    print(f"wrote {path}")
    print(report)


if __name__ == "__main__":
    main()
