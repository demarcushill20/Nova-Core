# TIMBOT — Client Trading System

Isolated client engagement. **No shared code with NovaTrade / IRB.** Self-contained
Pine Script `strategy()` deployed and validated entirely inside TradingView.

## Engagement

- **Client:** TIMBOT
- **Deliverable:** Working trading strategy, validated on a demo (paper) account.
- **Platform:** TradingView Pine Script v6 — strategy backtester + paper trading / alert-driven demo execution.
- **Started:** 2026-05-16

## Layout

```
clients/timbot/
  strategy/
    timbot_official_universal.pine   # the strategy source (v2.0)
  results/                           # backtest exports, demo trade logs, screenshots
  README.md
  DEMO_DEPLOYMENT.md                 # step-by-step demo validation plan
```

## Strategy summary

"TIMBOT OFFICIAL — Universal" — multi-timeframe ICT/liquidity strategy.

- **Entries:** 4H bias-flip driven (default `Signal Only On Bias Flip = true`), gated by
  session window, volume polarity, 15m/1h MACD, and an optional 5m RSI divergence filter.
- **Exits:** two-legged — 50% off at 2R, then break-even shift, runner to the next
  higher-timeframe 78.6% retracement level.
- **Universal TF:** auto-detects chart timeframe and picks the FROTE tier chain and
  one-above "sweep" timeframe accordingly.

## Validation target (locked 2026-05-17)

- **Instrument:** Gold — XAUUSD
- **Chart timeframe:** 15m  (chosen over 1h for a larger trade sample)
- **Derived by the script's auto-detection:**
  - Sweep TF = `60` (1h) — one step above the 15m chart
  - FROTE tier = 2 → chain `DAILY → WEEKLY`; runner targets the Weekly tier 78.6%
  - `pip` = `mintick` (XAUUSD `syminfo.type` is not `forex`, so no ×10 multiplier)

## Status

- [x] Project isolated under `clients/timbot/`
- [x] Strategy source filed (`strategy/timbot_official_universal.pine`)
- [x] Static Pine v6 syntax verified (request.security / strategy.exit / ta.macd / str.tostring / ta.change all v6-correct)
- [x] Validation target locked: XAUUSD 15m
- [ ] Compiles clean in TradingView Pine editor (paste check)
- [x] Backtest baseline captured — 5% sizing, 0 margin calls; see FINDINGS_XAUUSD_15m_5pct_2026-05-17.md (+4% / 5.5mo, modest)
  - void first run (100% sizing, 56 margin calls): FINDINGS_XAUUSD_15m_2026-05-17.md
- [x] Demo path = Path B: TradingView → MetaApi MT5 demo. See bridge/BRIDGE_DESIGN.md
- [x] Bridge deployed live on VPS — systemd `timbot-bridge`, DRY_RUN=false, MetaApi connected
- [x] Public endpoint — https://nova-link.duckdns.org/timbot/webhook (nginx :443 → 127.0.0.1:8080; bridge not publicly bound)
- [ ] TradingView webhook alert wired (operator — last step)
- [ ] Demo validation window complete

## Open items to verify before demo

Not a full audit (review was out of scope), but flag these to the client / check
during the compile + backtest pass:

1. **Daily-bias `request.security` calls** (`dHigh/dLow/dClose`) use `lookahead_on`
   on the *current* daily bar with no `[1]` offset — this can look ahead within the
   forming day. The Daily Bias filter is **off by default** (`useDailyBias = false`),
   so it does not affect default behaviour, but do not enable it without re-checking.
2. **`signalOnFlipOnly = true` (default):** entries fire purely on 4H bias flips and
   bypass the entire quality-score / displacement / sweep stack. Confirm with the
   client this is the intended default — the displacement/sweep machinery only
   affects the 🧹 visual signals and alerts in that mode, not entries.
3. **Backtest realism:** `commission_value = 0.01%` and no slippage are set. Add
   realistic slippage and per-instrument commission before trusting backtest stats.
4. **`calc_on_every_tick = false`:** backtest is bar-close based; demo/live behaviour
   on intrabar fills will differ. Expect minor divergence.

See `DEMO_DEPLOYMENT.md` for the full validation procedure.
