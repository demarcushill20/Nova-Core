# TIMBOT — Demo Deployment & Validation Plan

Goal: get `timbot_official_universal.pine` running on a **demo (paper) account**,
prove it behaves as designed, and hand the client a documented validation result.

Everything happens inside TradingView — no NovaCore engine, no shared infra.

---

## Phase 0 — Compile & sanity check

1. Open TradingView → Pine Editor → paste `strategy/timbot_official_universal.pine`.
2. **Add to chart.** Confirm it compiles with no errors. Record any warnings.
3. Pick the **demo instrument + timeframe** with the client. The script is
   "Universal" (auto-detects TF), but pick *one* primary pair/TF for validation —
   e.g. `EURUSD 15m` or `BTCUSD 1h`. Sweep TF and FROTE tiers derive from this.
4. Turn on **Debug Mode** input — the top-right table shows bias, gates, stops,
   and targets. Use it to confirm the engine state matches expectations.

**Gate:** does not proceed until it compiles clean and the debug table renders.

---

## Phase 1 — Backtest baseline

Capture a backtest *before* any demo run so demo results have something to compare to.

### Strategy settings to make the backtest realistic

In the strategy's **Properties** tab (and/or the `strategy()` header):

| Setting              | Default in script        | For validation                              |
|----------------------|--------------------------|----------------------------------------------|
| Initial capital      | 10,000                   | Match the demo account size                  |
| Order size           | 1% of equity             | Match the client's intended risk per trade   |
| Commission           | 0.01%                    | Set to the demo broker's real commission     |
| Slippage             | 0 ticks                  | Set 1–3 ticks (FX/crypto) — never leave at 0  |
| Pyramiding           | 0                        | Keep 0                                       |
| Recalculate          | on bar close             | Keep — `calc_on_every_tick = false`           |

### What to run

1. Run the **deep backtest** over at least 1–2 years (or max available history).
2. Export the **List of Trades** to CSV → save in `results/backtest_<pair>_<tf>_<date>.csv`.
3. Screenshot the **Performance Summary** → `results/backtest_<pair>_<tf>_<date>.png`.

### Metrics to record (the baseline)

- Net profit / return %
- Total closed trades
- Win rate %
- Profit factor
- Max drawdown %
- Avg trade, avg win / avg loss, largest losing trade
- Sharpe / Sortino (if shown)

**Gate:** if profit factor < 1 or max drawdown is unacceptable to the client,
stop and report — do not connect a demo account to a losing config.

---

## Phase 2 — Connect the demo account

TradingView does not auto-execute strategies. Two options for a demo:

### Option A — TradingView Paper Trading (simplest, recommended to start)

1. Bottom panel → **Trading Panel** → **Paper Trading** → connect.
2. Paper Trading fills from TradingView data — good for a quick behavioural check,
   but it does **not** auto-trade a strategy; signals must be placed manually or
   via alerts + a bridge. Use this only to eyeball signal timing.

### Option B — Alert → broker bridge (true hands-off demo)

1. The script already exposes `alertcondition()` calls: `Long Entry`, `Short Entry`,
   `Bias Flip BULL/BEAR`, `HTF Bull/Bear Sweep`.
2. For a *strategy* you instead create an alert on the **strategy itself**
   ("Any alert() function call" / order-fill events) so entries + exits both fire.
3. Point the alert webhook at the demo broker's endpoint (or a connector such as
   the broker's own TradingView integration / a webhook bridge). Confirm with the
   client which demo broker they want — that decides the webhook format.
4. Set the alert to **"Once Per Bar Close"** to match the bar-close strategy logic.

**Decision needed from client:** which demo broker / bridge. Until that's known,
validate with Option A.

---

## Phase 3 — Demo validation run

1. Run the demo for an agreed window — **minimum 2–4 weeks** or **≥ 30 trades**,
   whichever comes first (need enough trades for the result to mean anything).
2. Keep the chart + strategy untouched for the whole window (no parameter changes —
   any change restarts the sample).
3. Log every demo trade: timestamp, direction, entry, stop, both TP legs, exit
   reason, R result → `results/demo_trades_<date>.csv`.

### Daily / per-trade checks

- Entry fired only on a 4H bias flip (default mode) and inside the session window.
- 50% partial actually executed at ~2R.
- Stop moved to break-even after the partial.
- Runner exited at the HTF 78.6% target (or BE).
- Demo fill price vs. backtest signal price — note slippage.

---

## Phase 4 — Compare & report

Produce a short client-facing report in `results/`:

1. **Backtest vs demo table** — same metrics from Phase 1, side by side.
2. **Divergence analysis** — where demo differed from backtest and why
   (slippage, intrabar fills, missed alerts, broker rejects).
3. **Verdict** — does demo performance track the backtest within tolerance?
4. **Recommendation** — ready for the client's next step, or needs tuning.

**Acceptance criteria (agree these with the client up front):**

- Demo win rate within ~10% of backtest.
- Demo profit factor ≥ 1 and not drastically below backtest.
- No execution bugs (missed exits, duplicate orders, wrong sizing).
- Every demo entry traceable to a documented signal condition.

---

## Risk / scope notes

- This is a **demo-only** engagement. Do not connect a live-funded account without
  an explicit, separate client sign-off.
- Strategy parameters are the client's IP — do not change defaults without client
  approval; log any change and the reason in `results/`.
- Keep this engagement isolated: no code, data, or accounts shared with NovaTrade.
