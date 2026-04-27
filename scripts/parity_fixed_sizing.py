"""Diagnostic #4: re-run the Pine-aligned 10yr backtest at fixed 1-lot
sizing to isolate strategy logic from the equity-decay compounding artifact.

Why this matters: at 0.33% risk on a still-losing strategy, equity drains and
later trades size smaller — making the engine's per-trade $ figures hard to
compare to the Pine baseline (which effectively runs at fixed 1.0 lot because
its MAX_LOTS=1.0 cap binds on every IRB-sized stop). Fixing volume removes the
compounding decay so we can see whether the strategy's pip-by-pip behaviour
matches Pine, or whether the engine has a pip-level bug.

Outcome rule:
  PF ~> 1.0 here → strategy logic OK; live system just needs sizing review.
  PF stays < 1.0 → bug is in the engine (likely EMA-trail exit semantics).
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"


def parse_ts(s: str) -> float:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load(path: Path, sym: str, tf: str) -> list[Candle]:
    out: list[Candle] = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append(
                Candle(
                    timestamp=parse_ts(r["timestamp"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                    symbol=sym,
                    timeframe=tf,
                )
            )
    return sorted(out, key=lambda c: c.timestamp)


def run_scenario(label: str, h1: list[Candle], h4: list[Candle], cfg: StrategyConfig, **overrides) -> dict:
    env_kwargs = cfg.to_environment_kwargs()
    env_kwargs["use_ema_stack_filter"] = True

    base = BacktestEnvironment.for_v5_m5()
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    env = replace(
        base,
        primary_timeframe="H1",
        higher_timeframe="H4",
        h1_to_h4_ratio=4,
        h1_bars_per_day=24,
        h4_bars_per_day=6,
        **{**valid, **overrides},
    )
    bt = IRBBacktester(env=env)
    res = bt.run(h1, h4)
    trades = res.trades
    if not trades:
        return {"label": label, "trades": 0}
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    gross_w = sum(t.pnl_usd for t in wins)
    gross_l = -sum(t.pnl_usd for t in losses)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    net = sum(t.pnl_usd for t in trades)
    avg_win = gross_w / max(len(wins), 1)
    avg_loss = -gross_l / max(len(losses), 1)
    return {
        "label": label,
        "trades": len(trades),
        "wr": len(wins) / len(trades) * 100.0,
        "pf": pf,
        "net": net,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_w": gross_w,
        "gross_l": gross_l,
    }


def fmt(s: dict) -> str:
    if s["trades"] == 0:
        return f"  {s['label']:<48} (no trades)"
    return (
        f"  {s['label']:<48} "
        f"trades={s['trades']:5d}  WR={s['wr']:4.1f}%  PF={s['pf']:5.3f}  "
        f"Net=${s['net']:+10,.0f}  avg_win=${s['avg_win']:7,.0f}  avg_loss=${s['avg_loss']:7,.0f}"
    )


def main() -> None:
    print(f"[load] {H1_CSV.name}")
    h1 = load(H1_CSV, "EURUSD", "H1")
    print(f"        {len(h1):,} bars")
    h4 = aggregate_h1_to_h4(h1)
    print(f"[derive] H4: {len(h4):,} bars")

    cfg = StrategyConfig.from_yaml(CFG)

    print()
    print("Pine baseline (fixed 1-lot, 1% risk capped at MAX_LOTS=1.0):")
    print(
        "                                                  "
        "trades=1,204  WR=19.4%  PF=1.078  Net=+$8,887  avg_win=$+~104  avg_loss=$+~76"
    )
    print()
    print("Python scenarios (Pine-aligned config + ema-stack on):")

    scenarios: list[tuple[str, dict]] = [
        ("Current dynamic 0.33% risk (compounds)", {}),
        ("Fixed 1.0 lot (min=max=1.0)", {"min_volume": 1.0, "max_volume": 1.0}),
        ("Pine-equivalent sizing (1% risk, max=1.0 lot)", {"risk_fraction": 0.01, "max_volume": 1.0}),
        ("Fixed 1.0 lot + ATR floor 0", {"min_volume": 1.0, "max_volume": 1.0, "atr_sl_floor_multiplier": 0.0}),
        (
            "Fixed 1.0 lot + ATR floor 0 + spread buf 0",
            {"min_volume": 1.0, "max_volume": 1.0, "atr_sl_floor_multiplier": 0.0, "sl_spread_buffer_pips": 0.0},
        ),
    ]

    for label, overrides in scenarios:
        s = run_scenario(label, h1, h4, cfg, **overrides)
        print(fmt(s))

    print()
    print("Decision rule:")
    print("  - If a fixed-sizing scenario reaches PF >= 1.0 → strategy logic OK; the")
    print("    PF gap was the equity-decay artifact of dynamic risk sizing on a")
    print("    losing-during-warmup strategy. Live system needs sizing/cap review.")
    print("  - If all scenarios stay PF < 1.0 → bug is in the engine code, most likely")
    print("    in the EMA-trail exit boundary condition. Escalate to /brainstorm + plan.")


if __name__ == "__main__":
    main()
