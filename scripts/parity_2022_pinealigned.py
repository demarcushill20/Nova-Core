"""Re-run 2022 with parameters aligned to the Pine v5 source
(`configs/pinescript/irb_v5_stag.pine`) instead of the YAML config — to
quantify how much of the parity gap is explained by config drift.

Pine v5 hard-coded constants vs YAML config differences:
    ADX_THRESH:        20    (yaml: 0)         <-- sideways filter disabled in yaml
    MTF_H1_LOOKBACK:   20    (yaml: 1)         <-- weakest MTF filter setting
    EMA_FAST_PERIOD:   10    (yaml: 20)
    SLOPE_LOOKBACK:    20    (yaml: 5)
    OE_THRESH:         2.0   (yaml: 2.5)
    TRIGGER_WIN:       20    (yaml: 12)
    TIME_STOP:         40    (yaml: 20)
    EMA_CONFIRM_BARS:  3     (yaml: bypassed via use_simple_trend_filter)
"""

import csv
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CONFIG_PATH = REPO / "configs/strategies/irb_v5_m5_champion.yaml"


def parse_ts(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load(path, sym, tf, start_ts, end_ts):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            ts = parse_ts(r["timestamp"]) if not r["timestamp"].replace(".", "").isdigit() else float(r["timestamp"])
            if ts < start_ts or ts > end_ts:
                continue
            out.append(
                Candle(
                    timestamp=ts,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                    symbol=sym,
                    timeframe=tf,
                )
            )
    return out


def run_with_overrides(m5, h1, cfg, overrides, label):
    env_kwargs = cfg.to_environment_kwargs()
    env_kwargs.update(overrides)
    base = BacktestEnvironment.for_v5_m5()
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    env = replace(base, **valid)
    bt = IRBBacktester(env=env)
    res = bt.run(m5, h1)
    # Trades occurring in 2022 only
    jan22 = datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp()
    end22 = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()
    trades_22 = [t for t in res.trades if 0 <= t.entry_bar < len(m5) and jan22 <= m5[t.entry_bar].timestamp < end22]
    pnl = sum(t.pnl_usd for t in trades_22)
    wins = [t for t in trades_22 if t.pnl_usd > 0]
    losses = [t for t in trades_22 if t.pnl_usd < 0]
    gross_w = sum(t.pnl_usd for t in wins)
    gross_l = -sum(t.pnl_usd for t in losses)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    print(f"\n--- {label} ---")
    print(f"  Total trades 2022: {len(trades_22):,}  (Pine: 108)")
    print(f"  Net P&L:           ${pnl:,.0f}  (Pine: $11,034)")
    print(f"  Win rate:          {len(wins) / max(len(trades_22), 1) * 100:.1f}%   (Pine: 25.9%)")
    print(f"  Profit factor:     {pf:.2f}  (Pine: 2.06)")
    print(f"  Signals:           {len(res.signals):,}")
    rj = res.filter_rejections
    print(
        f"  Rejections: irb={rj.irb_geometry:,}  trend={rj.trend_filter:,}  "
        f"mtf={rj.mtf_alignment:,}  sideways={rj.sideways_filter:,}  "
        f"overext={rj.overextension_filter:,}"
    )
    return len(trades_22)


def main():
    start = datetime(2021, 12, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()
    print("Loading 2022 (with 1-month warmup) ...")
    m5 = load(M5_CSV, "EURUSD", "M5", start, end)
    h1 = load(H1_CSV, "EURUSD", "H1", start, end)
    print(f"  M5 bars: {len(m5):,}  H1 bars: {len(h1):,}")

    cfg = StrategyConfig.from_yaml(CONFIG_PATH)

    # Baseline (YAML config as-is)
    run_with_overrides(m5, h1, cfg, {}, "BASELINE (YAML config)")

    # Apply ADX_THRESH=20 only
    run_with_overrides(m5, h1, cfg, {"adx_threshold": 20.0}, "ADX≥20 only (Pine sideways filter)")

    # Apply MTF lookback 20 only
    run_with_overrides(m5, h1, cfg, {"mtf_lookback": 20}, "MTF lookback=20 only (Pine H1 trend confirmation)")

    # Apply OE threshold 2.0 only
    run_with_overrides(m5, h1, cfg, {"overextension_threshold": 2.0}, "OE_THRESH=2.0 only")

    # Disable simple trend, use slope filter w/ Pine settings (slope_lookback=20, slope_thresh=0.4)
    run_with_overrides(
        m5,
        h1,
        cfg,
        {
            "use_simple_trend_filter": False,
            "ema_fast_period": 10,
            "trend_slope_threshold": 0.4,
            "ema_confirm_bars": 3,
        },
        "Pine trend mode (EMA10 + slope≥0.4 + 3 confirm bars)",
    )

    # All Pine-aligned overrides combined
    run_with_overrides(
        m5,
        h1,
        cfg,
        {
            "adx_threshold": 20.0,
            "mtf_lookback": 20,
            "overextension_threshold": 2.0,
            "use_simple_trend_filter": False,
            "ema_fast_period": 10,
            "trend_slope_threshold": 0.4,
            "ema_confirm_bars": 3,
            "trigger_window_bars": 20,
            "time_stop_bars": 40,
        },
        "ALL Pine-aligned overrides",
    )


if __name__ == "__main__":
    main()
