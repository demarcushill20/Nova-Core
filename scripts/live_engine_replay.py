"""Drive `LiveStrategyEngine` over historical M5 EURUSD data.

The live engine's `_check_v5_exit` docstring claims it mirrors the vault Python
reference. This harness verifies that empirically: feed 10y of M5+H1 bars and
check whether the resulting trade ledger matches vault's profile (+$45k matched-
risk / WR ~49% / PF ~1.08 / 11-of-11 profitable years) — or whether it shares
the backtest engine's defects (-$102k / WR 34% / PF 0.50 / 0-of-11).

If the live engine matches vault: live trading is fine, no port needed.
If it matches the backtest engine: there's still real work ahead.

Usage:
    python3 scripts/live_engine_replay.py
"""

from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle
from novatrade.strategies.irb import IRBStrategy
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveSignal,
    LiveStrategyEngine,
    SignalType,
)

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_champion.yaml"
OUT_DIR = REPO / "OUTPUT"


def parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load_candles(path: Path, tf: str) -> list[Candle]:
    out: list[Candle] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            out.append(
                Candle(
                    timestamp=parse_ts(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    symbol="EURUSD",
                    timeframe=tf,
                )
            )
    out.sort(key=lambda c: c.timestamp)
    return out


@dataclass
class OpenTrade:
    side: str
    entry_ts: float
    entry_price: float
    initial_stop: float
    volume: float
    initial_volume: float
    partial_pnl_usd: float = 0.0
    legs: list[dict] = field(default_factory=list)


def pnl_usd(side: str, entry_px: float, exit_px: float, volume_lots: float, pip: float, lot_usd: float) -> float:
    pip_diff = (exit_px - entry_px) / pip if side == "LONG" else (entry_px - exit_px) / pip
    return pip_diff * volume_lots * lot_usd


def main() -> None:
    print(f"[load] {CFG.name}")
    cfg = StrategyConfig.from_yaml(CFG)
    env_kwargs = cfg.to_environment_kwargs()
    base = BacktestEnvironment.for_v5_m5()
    extras = {
        "primary_timeframe": "M5",
        "higher_timeframe": "H1",
        "h1_to_h4_ratio": 12,
        "h1_bars_per_day": 288,
        "h4_bars_per_day": 24,
    }
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    valid.update(extras)
    # Stage 3a port: honour NOVATRADE_STRATEGY_LOGIC env var so the same
    # replay can A/B test nova vs vault entry logic.
    strat_logic = (os.environ.get("NOVATRADE_STRATEGY_LOGIC") or "nova").strip().lower()
    if strat_logic == "vault":
        valid["vault_entry_logic"] = True
        valid["vault_pending_replace_any_side"] = True
        # Match vault Python matched-risk sizing cap (1 lot equivalent)
        valid["max_volume"] = 1.0
    env = dc_replace(base, **valid)
    print(
        f"[env] strategy_logic={strat_logic} · vault_entry_logic={env.vault_entry_logic} · "
        f"risk={env.risk_fraction * 100:.2f}% · partial={env.partial_exit_enabled} · "
        f"trail_atr_mult={env.trail_atr_multiplier} · time_stop={env.time_stop_bars}"
    )

    print("[load] M5+H1 candles...")
    m5 = load_candles(M5_CSV, "M5")
    h1 = load_candles(H1_CSV, "H1")

    # Optional window slice (REPLAY_YEARS=1 for quick test)
    years_env = os.environ.get("REPLAY_YEARS")
    if years_env:
        try:
            years = float(years_env)
            cutoff_ts = m5[-1].timestamp - years * 365.25 * 86400
            m5 = [b for b in m5 if b.timestamp >= cutoff_ts]
            h1 = [b for b in h1 if b.timestamp >= cutoff_ts]
            print(f"        REPLAY_YEARS={years} → window cutoff {datetime.utcfromtimestamp(cutoff_ts).date()}")
        except ValueError:
            pass
    print(f"        M5={len(m5):,}  H1={len(h1):,}")

    # Build engine — vault_native uses VaultLiveEngine, others use LiveStrategyEngine
    live_cfg = LiveConfig(
        symbol=env.symbol_display,
        primary_timeframe="M5",
        higher_timeframe="H1",
        trigger_window_bars=env.trigger_window_bars,
        enable_market_hours_check=False,
    )
    if strat_logic == "vault_native":
        from novatrade.strategy.vault_engine import VaultLiveEngine

        engine = VaultLiveEngine(env=env, config=live_cfg)
        print("[engine] VaultLiveEngine (vault_native) — drives IRBStrategyState")
    else:
        strategy = IRBStrategy(env=env)
        engine = LiveStrategyEngine(strategy=strategy, env=env, config=live_cfg)  # type: ignore[assignment]
        print(f"[engine] LiveStrategyEngine ({strat_logic}) — IRBStrategy")

    # Seed with warmup bars
    warmup_n = max(env.warmup_bars, 60)
    seed_m5 = m5[:warmup_n]
    # H1 bars whose timestamp <= last seeded M5 bar timestamp
    seed_cutoff = seed_m5[-1].timestamp
    seed_h1 = [b for b in h1 if b.timestamp <= seed_cutoff]
    engine.seed_history(seed_m5, seed_h1)
    print(f"[seed] m5={len(seed_m5)}  h1={len(seed_h1)}  state={engine.state.value}")

    # Replay loop
    open_trade: OpenTrade | None = None
    completed_trades: list[dict] = []
    pip = env.pip_value
    lot_usd = env.pip_value_per_standard_lot

    # Build interleaved event stream: H1 bars first when timestamps tie
    h1_iter = iter([b for b in h1 if b.timestamp > seed_cutoff])
    next_h1: Candle | None = next(h1_iter, None)

    n_signals_by_type: Counter[str] = Counter()
    bars_processed = 0
    log_every = max(1, (len(m5) - warmup_n) // 10)

    for j, m5_bar in enumerate(m5[warmup_n:]):
        # Push any H1 bars that closed before or at this M5 bar
        while next_h1 is not None and next_h1.timestamp <= m5_bar.timestamp:
            engine.on_bar(next_h1, "H1")
            next_h1 = next(h1_iter, None)

        signals = engine.on_bar(m5_bar, "M5")
        bars_processed += 1
        for sig in signals:
            n_signals_by_type[sig.signal_type.name] += 1
            ts = sig.metadata.get("bar_close_time", m5_bar.timestamp)
            handle_signal(sig, ts, engine, open_trade, completed_trades, pip, lot_usd)  # type: ignore[arg-type]
            # open_trade gets updated via the handler — re-fetch from engine state
            if engine.position is None:
                open_trade = None
            elif open_trade is None and engine.position is not None:
                # New fill happened
                pos = engine.position
                open_trade = OpenTrade(
                    side=pos.side,
                    entry_ts=ts,
                    entry_price=pos.entry_price,
                    initial_stop=pos.initial_stop,
                    volume=pos.volume,
                    initial_volume=pos.initial_volume or pos.volume,
                )

        # Check if a fill JUST happened that we missed (safety net)
        if open_trade is None and engine.position is not None:
            pos = engine.position
            open_trade = OpenTrade(
                side=pos.side,
                entry_ts=m5_bar.timestamp,
                entry_price=pos.entry_price,
                initial_stop=pos.initial_stop,
                volume=pos.volume,
                initial_volume=pos.initial_volume or pos.volume,
            )

        if (j + 1) % log_every == 0:
            elapsed_ts = m5_bar.timestamp
            year = datetime.utcfromtimestamp(elapsed_ts).date()
            print(
                f"  [progress] m5_bar #{j + 1:,}/{len(m5) - warmup_n:,}  date={year}  "
                f"signals={sum(n_signals_by_type.values())}  trades_completed={len(completed_trades)}  "
                f"equity=${engine.equity:,.2f}"
            )

    print()
    print(f"[done] bars_processed={bars_processed:,}  signals={dict(n_signals_by_type)}")
    print(f"        completed_trades={len(completed_trades):,}  final_equity=${engine.equity:,.2f}")

    if not completed_trades:
        print("No trades. Aborting report.")
        return

    # ----- Report -----
    init_eq = env.initial_equity
    pnls = [t["pnl"] for t in completed_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = sum(wins) / -sum(losses) if losses else (float("inf") if wins else 0.0)

    # equity curve & drawdown
    eq = init_eq
    peak = init_eq
    max_dd = 0.0
    max_dd_pct = 0.0
    for t in completed_trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100.0 if peak else 0.0

    by_year: dict[int, list[float]] = defaultdict(list)
    for t in completed_trades:
        y = datetime.utcfromtimestamp(t["exit_ts"]).year
        by_year[y].append(t["pnl"])
    prof_yrs = sum(1 for y, ps in by_year.items() if sum(ps) > 0)

    exit_mix = Counter(t["exit_reason"] for t in completed_trades)

    lines: list[str] = []
    p = lines.append
    start_date = datetime.utcfromtimestamp(m5[warmup_n].timestamp).date()
    end_date = datetime.utcfromtimestamp(m5[-1].timestamp).date()
    p(f"# Live engine replay — {start_date} → {end_date}")
    p("")
    p("**Engine:** `novatrade.strategy.live_engine.LiveStrategyEngine` (the live runtime)")
    p("**Strategy:** `IRBStrategy` v5 (champion config)")
    p(
        f"**Risk:** {env.risk_fraction * 100:.2f}% · max_trades_day="
        f"{env.max_trades_per_day} · cooldown={env.cooldown_bars}"
    )
    p("")
    p("## Headline")
    p(f"- Net P&L:        ${sum(pnls):+,.2f}  ({sum(pnls) / init_eq * 100:+.2f}%)")
    p(f"- Final equity:   ${init_eq + sum(pnls):,.2f}")
    p(f"- Trades:         {len(completed_trades):,}")
    p(f"- Win rate:       {len(wins) / max(1, len(completed_trades)) * 100:.2f}%")
    p(f"- Profit factor:  {pf:.3f}")
    p(f"- Max DD:         ${-max_dd:,.2f}  ({max_dd_pct:.2f}% of peak)")
    p(f"- Profitable yrs: {prof_yrs}/{len(by_year)}")
    p("")
    p("## Year-by-Year")
    p("")
    p("| Year | Trades | Net P&L | PF |")
    p("|---:|---:|---:|---:|")
    for y in sorted(by_year):
        ps = by_year[y]
        wL = sum(p_ for p_ in ps if p_ > 0)
        lL = -sum(p_ for p_ in ps if p_ < 0)
        ypf = (wL / lL) if lL else (float("inf") if wL else 0.0)
        ypf_s = "∞" if ypf == float("inf") else f"{ypf:.2f}"
        p(f"| {y} | {len(ps)} | ${sum(ps):+,.2f} | {ypf_s} |")
    p("")
    p("## Exit-reason mix")
    for r, n in exit_mix.most_common():
        p(f"- {r}: {n} ({n / len(completed_trades) * 100:.1f}%)")
    p("")
    p("**[ref] vault matched-risk:** trades=32,815 · net=+$45,310 · WR 49.34% · PF 1.081 · DD 6.87% · 11/11 yrs")
    p("**[ref] nova backtest engine:** trades=19,685 · net=-$101,957 · WR 33.90% · PF 0.504 · DD 101.9% · 0/11 yrs")

    report = "\n".join(lines)
    print()
    print(report)

    OUT_DIR.mkdir(exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_md = OUT_DIR / f"backtest_live_engine_replay_{ts_tag}.md"
    out_md.write_text(report)
    out_csv = OUT_DIR / f"backtest_live_engine_replay_{ts_tag}_trades.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["entry_ts", "side", "entry_price", "exit_ts", "exit_price", "volume", "pnl", "exit_reason"],
        )
        w.writeheader()
        for t in completed_trades:
            w.writerow(t)
    print(f"\n[saved] {out_md}")
    print(f"[saved] {out_csv}")


def handle_signal(
    sig: LiveSignal,
    ts: float,
    engine: LiveStrategyEngine,
    open_trade: OpenTrade | None,
    completed_trades: list[dict],
    pip: float,
    lot_usd: float,
) -> None:
    """Translate a live signal into ledger updates."""
    # Most state is maintained by the engine. We only need to:
    # - On PARTIAL_EXIT: accumulate partial PnL on the open trade
    # - On EXIT: close the trade, record total PnL, call engine.notify_close
    if sig.signal_type == SignalType.PARTIAL_EXIT:
        pos = engine.position
        if pos is None:
            return
        partial_pnl = pnl_usd(
            pos.side, pos.entry_price, sig.exit_price or pos.entry_price, sig.volume or 0.0, pip, lot_usd
        )
        # Stash on the engine's position via attribute (we'll use it on EXIT)
        if not hasattr(pos, "_replay_partial_pnl"):
            pos._replay_partial_pnl = 0.0  # type: ignore[attr-defined]
        pos._replay_partial_pnl += partial_pnl  # type: ignore[attr-defined]
    elif sig.signal_type == SignalType.EXIT:
        pos = engine.position
        if pos is None:
            return
        # CRITICAL: pos.volume reflects the CURRENT open size (updated after
        # partial fires at live_engine.py:905). Pre-partial: pos.volume is
        # initial volume = full position. Post-partial: pos.volume = runner_qty.
        # Using pos.runner_qty_open under-counts pre-partial trades by 50%.
        exit_volume = pos.volume
        runner_pnl = pnl_usd(pos.side, pos.entry_price, sig.exit_price or pos.entry_price, exit_volume, pip, lot_usd)
        partial_pnl = getattr(pos, "_replay_partial_pnl", 0.0)
        # Zero-cost accounting to match vault Python reference (+$45k matched-risk).
        # Vault's number doesn't apply spread/slippage/commission. For LIVE-realism
        # comparison, costs would be applied here.
        total_pnl = partial_pnl + runner_pnl
        completed_trades.append(
            {
                "entry_ts": pos.entry_bar,  # bar index; we'll fix up below
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_ts": ts,
                "exit_price": sig.exit_price,
                "volume": pos.initial_volume,
                "pnl": total_pnl,
                "exit_reason": sig.exit_reason or "unknown",
            }
        )
        engine.notify_close(total_pnl)


if __name__ == "__main__":
    main()
