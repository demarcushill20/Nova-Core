#!/usr/bin/env python3
"""
public_demo_trend.py — Reproduction scaffold for the Public.com multi-asset
trend-following backtest demo described in the source transcript (task 1074).

The transcript segment describes the Public.com API auth + data flow:

    secret key  --(POST)-->  access token (short-lived, e.g. 30 min)
    access token --(GET)-->  historical bar data for any symbol

and the pitch: stocks + ETFs + commodities + crypto from ONE endpoint, so a
trend follower can diversify across asset classes without juggling 3-4 APIs.

This file mirrors that flow so the demo is *reproducible on their data*, while
the strategy logic and verdicts are NovaCore's own (per the operator's note:
"all my opinions are expressed as my own").

SAFETY / HONESTY NOTES
----------------------
- The secret key is read from the PUBLIC_SECRET_KEY env var. NEVER hard-code it
  or commit it (the transcript itself warns about leaked keys — that is why the
  intermediate access token expires).
- Endpoint paths below are the *described* shape, not verified against live API
  docs. Set PUBLIC_API_BASE / token + bar paths from the real docs
  (public.com/go -> API docs) before running against production. Run with
  --dry-run (default) to exercise the strategy on synthetic bars with no network.

Usage:
    export PUBLIC_SECRET_KEY=...            # from your Public account
    python scripts/public_demo_trend.py --symbols SPY,GLD,BTC-USD --dry-run
    python scripts/public_demo_trend.py --symbols SPY,GLD,BTC-USD --live
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field

PUBLIC_API_BASE = os.environ.get("PUBLIC_API_BASE", "https://api.public.com")
# Paths are the transcript-described shape; confirm against live docs before --live.
TOKEN_PATH = os.environ.get("PUBLIC_TOKEN_PATH", "/userapiauthservice/personal/access-tokens")
BARS_PATH = os.environ.get("PUBLIC_BARS_PATH", "/marketdata/{symbol}/bars")


# --------------------------------------------------------------------------- #
# Data access (mirrors the transcript's secret-key -> access-token -> bars flow)
# --------------------------------------------------------------------------- #
def get_access_token(secret_key: str) -> str:
    """Exchange the long-lived secret key for a short-lived access token."""
    import requests  # imported lazily so --dry-run needs no deps

    resp = requests.post(
        f"{PUBLIC_API_BASE}{TOKEN_PATH}",
        json={"secret": secret_key, "validityInMinutes": 30},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def fetch_bars(token: str, symbol: str, timeframe: str = "1day", limit: int = 500):
    """Pull historical bars for a single symbol. Returns list[float] of closes."""
    import requests

    resp = requests.get(
        f"{PUBLIC_API_BASE}{BARS_PATH.format(symbol=symbol)}",
        headers={"Authorization": f"Bearer {token}"},
        params={"timeframe": timeframe, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    return [float(b["close"]) for b in resp.json()["bars"]]


def synthetic_bars(symbol: str, n: int = 500) -> list[float]:
    """Deterministic trend+noise series so --dry-run runs offline & reproducibly."""
    seed = sum(ord(c) for c in symbol)
    drift = 0.0004 * (1 if seed % 2 == 0 else -1)
    price, out = 100.0, []
    for i in range(n):
        # deterministic pseudo-noise (no RNG, so output is stable across runs)
        noise = 0.01 * math.sin((i + seed) * 0.37) + 0.006 * math.cos((i + seed) * 1.13)
        price *= 1 + drift + noise
        out.append(round(price, 4))
    return out


# --------------------------------------------------------------------------- #
# Strategy — NovaCore's own logic (classic Donchian-style trend following)
# --------------------------------------------------------------------------- #
@dataclass
class TrendResult:
    symbol: str
    n_bars: int
    trades: int
    total_return: float
    sharpe: float
    equity: list[float] = field(default_factory=list)


def trend_follow(closes: list[float], fast: int = 20, slow: int = 100) -> TrendResult:
    """
    Long-only dual-moving-average trend filter (the canonical 'does it trend?'
    test the transcript motivates). Position = +1 when fast SMA > slow SMA.
    This is NovaCore's strategy expression, not the creator's code.
    """
    if len(closes) <= slow:
        return TrendResult("", len(closes), 0, 0.0, 0.0)

    def sma(series, w, i):
        return sum(series[i - w + 1 : i + 1]) / w

    equity, pos, trades, rets = [1.0], 0, 0, []
    for i in range(slow, len(closes)):
        signal = 1 if sma(closes, fast, i) > sma(closes, slow, i) else 0
        if signal != pos:
            trades += 1
            pos = signal
        bar_ret = (closes[i] / closes[i - 1]) - 1.0
        strat_ret = pos * bar_ret
        rets.append(strat_ret)
        equity.append(equity[-1] * (1 + strat_ret))

    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0
    return TrendResult("", len(closes), trades, equity[-1] - 1.0, sharpe, equity)


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbols", default="SPY,GLD,BTC-USD,DBC", help="comma-separated, mixing asset classes for diversification"
    )
    ap.add_argument("--timeframe", default="1day")
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=100)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True, help="(default) synthetic bars, no network, no key needed"
    )
    mode.add_argument("--live", dest="dry_run", action="store_false", help="hit Public.com using PUBLIC_SECRET_KEY")
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    token = None
    if not args.dry_run:
        key = os.environ.get("PUBLIC_SECRET_KEY")
        if not key:
            print("ERROR: --live needs PUBLIC_SECRET_KEY in the environment.", file=sys.stderr)
            return 2
        token = get_access_token(key)

    results = []
    for sym in symbols:
        closes = synthetic_bars(sym) if args.dry_run else fetch_bars(token, sym, args.timeframe)
        r = trend_follow(closes, args.fast, args.slow)
        r.symbol = sym
        results.append(r)

    print(
        f"\nTrend-following demo  (mode={'DRY-RUN/synthetic' if args.dry_run else 'LIVE/public.com'}, "
        f"MA {args.fast}/{args.slow})\n" + "-" * 64
    )
    print(f"{'symbol':<10}{'bars':>6}{'trades':>8}{'return':>10}{'sharpe':>9}")
    for r in results:
        print(f"{r.symbol:<10}{r.n_bars:>6}{r.trades:>8}{r.total_return * 100:>9.1f}%{r.sharpe:>9.2f}")

    # Diversified equal-weight portfolio — the transcript's whole thesis is that
    # you don't know *which* market trends, so you spread across asset classes.
    valid = [r for r in results if r.equity]
    if len(valid) > 1:
        port = sum(r.total_return for r in valid) / len(valid)
        avg_sharpe = sum(r.sharpe for r in valid) / len(valid)
        print("-" * 64)
        print(f"{'PORTFOLIO':<10}{'':>6}{'':>8}{port * 100:>9.1f}%{avg_sharpe:>9.2f}  (equal-weight)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
